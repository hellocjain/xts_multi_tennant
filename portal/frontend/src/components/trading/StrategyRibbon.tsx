import React, { useState, useEffect, useRef } from 'react';
import { StrategyItem } from '../../types/telemetry';
import { 
  Plus, 
  Play, 
  Square, 
  RefreshCw, 
  Power, 
  Trash2, 
  RotateCw
} from 'lucide-react';
import { toast } from 'sonner';

interface StrategyRibbonProps {
  strategies: StrategyItem[];
  selectedStrategyId?: string;
  onSelectStrategy: (strat: StrategyItem) => void;
  onToggleStrategy: (stratId: string) => Promise<void>;
  onEvaluateNow: (strat: StrategyItem) => Promise<void>;
  onSyncTrend: (strat: StrategyItem, trend: 'BUY' | 'SELL' | 'FLAT') => Promise<void>;
  onResetFlat: (strat: StrategyItem) => Promise<void>;
  onDeleteStrategy: (stratId: string) => Promise<void>;
  onAddStrategyClick: () => void;
}

export const StrategyRibbon: React.FC<StrategyRibbonProps> = ({
  strategies,
  selectedStrategyId,
  onSelectStrategy,
  onToggleStrategy,
  onEvaluateNow,
  onSyncTrend,
  onResetFlat,
  onDeleteStrategy,
  onAddStrategyClick,
}) => {
  const [isSyncing, setIsSyncing] = useState(false);
  const [isEvaluating, setIsEvaluating] = useState(false);
  const [isResetting, setIsResetting] = useState(false);
  const [isToggling, setIsToggling] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [showSyncDropdown, setShowSyncDropdown] = useState(false);
  const syncDropdownRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (syncDropdownRef.current && !syncDropdownRef.current.contains(event.target as Node)) {
        setShowSyncDropdown(false);
      }
    };
    if (showSyncDropdown) {
      document.addEventListener('mousedown', handleClickOutside);
    }
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [showSyncDropdown]);

  const selectedStrat = strategies.find((s) => s.id === selectedStrategyId) || strategies[0];

  const handleEvaluate = async () => {
    if (!selectedStrat || isEvaluating) return;
    setIsEvaluating(true);
    try {
      await onEvaluateNow(selectedStrat);
      toast.success(`Evaluated ${selectedStrat.symbol} (${selectedStrat.timeframe}) successfully`);
    } catch (err: any) {
      toast.error(`Evaluation failed: ${err.message}`);
    } finally {
      setIsEvaluating(false);
    }
  };

  const handleSync = async (targetTrend: 'BUY' | 'SELL' | 'FLAT') => {
    if (!selectedStrat || isSyncing) return;
    setShowSyncDropdown(false);
    setIsSyncing(true);
    try {
      await onSyncTrend(selectedStrat, targetTrend);
      toast.success(`Synced trend of ${selectedStrat.symbol} to ${targetTrend}`);
    } catch (err: any) {
      toast.error(`Sync trend failed: ${err.message}`);
    } finally {
      setIsSyncing(false);
    }
  };

  const handleReset = async () => {
    if (!selectedStrat || isResetting) return;
    setIsResetting(true);
    try {
      await onResetFlat(selectedStrat);
      toast.success(`Reset ${selectedStrat.symbol} to FLAT`, {
        description: 'Virtual position set to 0 and trend reset.',
      });
    } catch (err: any) {
      toast.error(`Reset flat failed: ${err.message}`);
    } finally {
      setIsResetting(false);
    }
  };

  return (
    <div className="bg-cardbg rounded-xl border border-bordercolor p-3 space-y-3">
      {/* Strategy Pills List */}
      <div className="flex items-center justify-between">
        <div className="flex items-center space-x-2 overflow-x-auto no-scrollbar py-0.5">
          {strategies.length === 0 ? (
            <div className="text-xs text-slate-400 italic">No strategies configured for this client.</div>
          ) : (
            strategies.map((strat) => {
              const isSelected = selectedStrat && strat.id === selectedStrat.id;
              const pos = strat.virtual_position || 0;
              const isLong = pos > 0;
              const isShort = pos < 0;

              return (
                <button
                  key={strat.id}
                  type="button"
                  onClick={() => onSelectStrategy(strat)}
                  className={`flex items-center space-x-2 px-3 py-1.5 rounded-lg border text-xs font-mono transition shrink-0 ${
                    isSelected
                      ? 'bg-brand-600/15 border-brand-500 text-slate-100 shadow-sm'
                      : 'bg-obsidian border-bordercolor text-slate-400 hover:border-slate-700 hover:text-slate-200'
                  }`}
                >
                  <span
                    className={`w-2 h-2 rounded-full ${
                      strat.is_enabled ? 'bg-emerald-400 animate-pulse' : 'bg-slate-600'
                    }`}
                  />
                  <span className="font-bold text-slate-200">{strat.symbol}</span>
                  <span className="text-[10px] text-slate-400">({strat.timeframe})</span>

                  {/* Position Pill */}
                  <span
                    className={`px-1.5 py-0.2 rounded text-[10px] font-bold ${
                      isLong
                        ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
                        : isShort
                        ? 'bg-rose-500/20 text-rose-400 border border-rose-500/30'
                        : 'bg-slate-800 text-slate-400'
                    }`}
                  >
                    {isLong ? `+${pos} L` : isShort ? `${pos} S` : 'FLAT'}
                  </span>
                </button>
              );
            })
          )}

          <button
            type="button"
            onClick={onAddStrategyClick}
            title="Add Strategy"
            className="flex items-center space-x-1 px-2.5 py-1.5 rounded-lg border border-dashed border-slate-700 text-slate-400 hover:text-brand-400 hover:border-brand-500/50 text-xs font-mono transition shrink-0"
          >
            <Plus className="w-3.5 h-3.5" />
            <span>Add Strategy</span>
          </button>
        </div>
      </div>

      {/* Selected Strategy Controls Bar */}
      {selectedStrat && (
        <div className="pt-2 border-t border-bordercolor flex flex-wrap items-center justify-between gap-3 text-xs">
          {/* Strategy Meta Parameters */}
          <div className="flex items-center space-x-3 text-[11px] font-mono text-slate-400">
            <div>
              <span>Mode:</span>{' '}
              <strong className={selectedStrat.execution_mode === 'LIVE' ? 'text-emerald-400' : 'text-amber-400'}>
                {selectedStrat.execution_mode || 'LIVE'}
              </strong>
            </div>
            <div>
              <span>Lots:</span> <strong className="text-slate-200">{selectedStrat.quantity}</strong>
            </div>
            <div>
              <span>ATR:</span> <strong className="text-slate-200">{selectedStrat.atr_period}</strong>
            </div>
            <div>
              <span>Mult:</span> <strong className="text-slate-200">{selectedStrat.multiplier}</strong>
            </div>
            {selectedStrat.active_contract_desc && (
              <div>
                <span>Contract:</span> <strong className="text-brand-400">{selectedStrat.active_contract_desc}</strong>
              </div>
            )}
          </div>

          {/* Quick Actions */}
          <div className="flex items-center space-x-2">
            {/* Sync Trend Dropdown */}
            <div className="relative" ref={syncDropdownRef}>
              <button
                type="button"
                onClick={() => setShowSyncDropdown(!showSyncDropdown)}
                disabled={isSyncing}
                title="Override and Synchronize Strategy Trend"
                className="flex items-center space-x-1.5 px-3 py-1.5 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded-lg text-slate-200 font-medium transition"
              >
                <RefreshCw className={`w-3.5 h-3.5 ${isSyncing ? 'animate-spin text-brand-400' : ''}`} />
                <span>Sync Trend</span>
              </button>

              {showSyncDropdown && (
                <div className="absolute right-0 mt-1.5 w-44 bg-obsidian border border-bordercolor rounded-xl shadow-2xl p-1.5 z-30 space-y-1 animate-in fade-in zoom-in-95 duration-100">
                  <div className="px-2 py-1 text-[10px] font-mono text-slate-400 uppercase tracking-wider">
                    Target Trend Override
                  </div>
                  <button
                    type="button"
                    onClick={() => handleSync('BUY')}
                    className="w-full text-left px-2.5 py-1.5 text-xs font-mono text-emerald-400 hover:bg-emerald-500/10 rounded-lg transition font-semibold"
                  >
                    Force BUY (+LONG)
                  </button>
                  <button
                    type="button"
                    onClick={() => handleSync('SELL')}
                    className="w-full text-left px-2.5 py-1.5 text-xs font-mono text-rose-400 hover:bg-rose-500/10 rounded-lg transition font-semibold"
                  >
                    Force SELL (-SHORT)
                  </button>
                  <button
                    type="button"
                    onClick={() => handleSync('FLAT')}
                    className="w-full text-left px-2.5 py-1.5 text-xs font-mono text-slate-300 hover:bg-slate-800 rounded-lg transition"
                  >
                    Force FLAT (0)
                  </button>
                </div>
              )}
            </div>

            {/* Evaluate Now */}
            <button
              type="button"
              onClick={handleEvaluate}
              disabled={isEvaluating}
              title="Trigger Instant Strategy Evaluation"
              className="flex items-center space-x-1.5 px-3 py-1.5 bg-brand-600 hover:bg-brand-500 border border-brand-500 rounded-lg text-white font-medium transition shadow-sm"
            >
              <Play className={`w-3.5 h-3.5 fill-current ${isEvaluating ? 'animate-spin' : ''}`} />
              <span>Evaluate</span>
            </button>

            {/* Reset FLAT */}
            <button
              type="button"
              onClick={handleReset}
              disabled={isResetting}
              title="Reset Virtual State to FLAT"
              className="flex items-center space-x-1 px-2.5 py-1.5 bg-slate-900 hover:bg-slate-800 border border-bordercolor rounded-lg text-slate-300 transition"
            >
              <Square className="w-3.5 h-3.5 text-amber-400" />
              <span>Reset FLAT</span>
            </button>

            {/* Enable/Disable Toggle */}
            <button
              type="button"
              disabled={isToggling}
              onClick={async () => {
                setIsToggling(true);
                try {
                  await onToggleStrategy(selectedStrat.id);
                } finally {
                  setIsToggling(false);
                }
              }}
              title={selectedStrat.is_enabled ? 'Disable Strategy' : 'Enable Strategy'}
              className={`p-1.5 rounded-lg border transition ${
                selectedStrat.is_enabled
                  ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400 hover:bg-emerald-500/20'
                  : 'bg-slate-800 border-slate-700 text-slate-400 hover:text-slate-200'
              }`}
            >
              {isToggling ? <RotateCw className="w-3.5 h-3.5 animate-spin" /> : <Power className="w-3.5 h-3.5" />}
            </button>

            {/* Delete Strategy */}
            <button
              type="button"
              disabled={isDeleting}
              onClick={async () => {
                setIsDeleting(true);
                try {
                  await onDeleteStrategy(selectedStrat.id);
                  toast.success(`Removed strategy ${selectedStrat.symbol} (${selectedStrat.timeframe})`);
                } finally {
                  setIsDeleting(false);
                }
              }}
              title="Delete Strategy"
              className="p-1.5 rounded-lg text-slate-400 hover:text-rose-400 hover:bg-rose-500/10 border border-transparent hover:border-rose-500/20 transition"
            >
              {isDeleting ? <RotateCw className="w-3.5 h-3.5 animate-spin text-rose-400" /> : <Trash2 className="w-3.5 h-3.5" />}
            </button>
          </div>
        </div>
      )}
    </div>
  );
};
