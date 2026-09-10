import React, { useState, useEffect } from 'react';
import { Flame, Activity, ShieldAlert, LogOut, Clock, Wifi, WifiOff, RotateCw } from 'lucide-react';
import { DashboardTelemetry } from '../../types/telemetry';

interface HeaderProps {
  telemetry?: DashboardTelemetry;
  isWsConnected: boolean;
  onOpenGlobalPanic: () => void;
  onLogout: () => Promise<void> | void;
}

export const Header: React.FC<HeaderProps> = ({
  telemetry,
  isWsConnected,
  onOpenGlobalPanic,
  onLogout,
}) => {
  const [istTime, setIstTime] = useState<string>('');
  const [isLoggingOut, setIsLoggingOut] = useState(false);

  useEffect(() => {
    const updateTime = () => {
      const now = new Date();
      // Indian Standard Time (UTC + 5:30)
      const istString = now.toLocaleTimeString('en-US', {
        timeZone: 'Asia/Kolkata',
        hour12: false,
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      });
      setIstTime(istString);
    };
    updateTime();
    const timer = setInterval(updateTime, 1000);
    return () => clearInterval(timer);
  }, []);

  const netMtm = telemetry?.aggregate_net_mtm ?? 0;
  const isProfit = netMtm >= 0;
  const mcxOpen = telemetry?.market_status?.mcx_open ?? false;
  const nseOpen = telemetry?.market_status?.nse_open ?? false;

  const handleLogoutClick = async () => {
    if (isLoggingOut) return;
    setIsLoggingOut(true);
    try {
      await onLogout();
    } finally {
      setIsLoggingOut(false);
    }
  };

  return (
    <header className="h-14 bg-cardbg border-b border-bordercolor px-4 flex items-center justify-between shrink-0 select-none z-30">
      {/* Left: Brand & Market Session Badges */}
      <div className="flex items-center space-x-3">
        <div className="flex items-center space-x-2">
          <div className="w-8 h-8 rounded-xl bg-brand-600/20 border border-brand-500/30 flex items-center justify-center text-brand-400 font-bold font-mono text-sm shadow-inner">
            X
          </div>
          <div>
            <div className="flex items-center gap-1.5">
              <span className="font-bold text-xs tracking-wider text-slate-100 uppercase">XTS TERMINAL</span>
              <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-slate-800 text-slate-400 border border-slate-700">v2.4</span>
            </div>
          </div>
        </div>

        <div className="h-5 w-[1px] bg-bordercolor hidden sm:block"></div>

        {/* Live Market Clocks */}
        <div className="hidden sm:flex items-center space-x-2 text-xs">
          <div className="flex items-center space-x-1 font-mono text-slate-300 bg-obsidian/70 px-2 py-1 rounded-lg border border-bordercolor">
            <Clock className="w-3.5 h-3.5 text-slate-400" />
            <span>{istTime || '--:--:--'} IST</span>
          </div>

          <div className="flex items-center space-x-1.5 px-2 py-1 rounded-lg bg-obsidian/50 border border-bordercolor text-[11px] font-mono">
            <span className="text-slate-400 font-semibold">MCX</span>
            <span className={`w-2 h-2 rounded-full ${mcxOpen ? 'bg-emerald-500 animate-pulse' : 'bg-rose-500'}`} />
          </div>

          <div className="flex items-center space-x-1.5 px-2 py-1 rounded-lg bg-obsidian/50 border border-bordercolor text-[11px] font-mono">
            <span className="text-slate-400 font-semibold">NSE</span>
            <span className={`w-2 h-2 rounded-full ${nseOpen ? 'bg-emerald-500 animate-pulse' : 'bg-rose-500'}`} />
          </div>
        </div>
      </div>

      {/* Center: Live Aggregate Portfolio MTM Ticker */}
      <div className="flex items-center space-x-3">
        <div className="bg-obsidian/90 border border-bordercolor rounded-xl px-3.5 py-1 flex items-center space-x-3 shadow-inner">
          <div className="flex items-center space-x-1.5">
            <Activity className={`w-3.5 h-3.5 ${isProfit ? 'text-emerald-400' : 'text-rose-400'} animate-pulse`} />
            <span className="text-[11px] text-slate-400 font-medium uppercase tracking-wider hidden md:inline">PORTFOLIO MTM</span>
          </div>
          <div className={`font-mono text-xs md:text-sm font-bold tracking-tight ${isProfit ? 'text-emerald-400' : 'text-rose-400'}`}>
            {isProfit ? '+' : ''}₹{netMtm.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </div>
        </div>

        {/* WebSocket Health Status */}
        <div
          title={isWsConnected ? 'WebSocket Live Push Active' : 'Fallback REST Polling Active'}
          className="flex items-center gap-1 text-[11px] font-mono px-2 py-1 rounded-lg bg-obsidian/40 border border-bordercolor text-slate-400"
        >
          {isWsConnected ? (
            <>
              <Wifi className="w-3 h-3 text-emerald-400" />
              <span className="hidden xl:inline text-emerald-400">WS Live</span>
            </>
          ) : (
            <>
              <WifiOff className="w-3 h-3 text-amber-400" />
              <span className="hidden xl:inline text-amber-400">Polling</span>
            </>
          )}
        </div>
      </div>

      {/* Right: Emergency Panic Button & Actions */}
      <div className="flex items-center space-x-2.5">
        <button
          type="button"
          onClick={onOpenGlobalPanic}
          className="bg-rose-600 hover:bg-rose-500 active:bg-rose-700 text-white font-bold text-xs uppercase tracking-wider px-3.5 py-1.5 rounded-xl transition shadow-lg shadow-rose-600/30 flex items-center gap-1.5 cursor-pointer"
        >
          <Flame className="w-3.5 h-3.5" />
          <span className="hidden sm:inline">PANIC SQUARE OFF ALL</span>
          <span className="sm:hidden">PANIC ALL</span>
        </button>

        <button
          type="button"
          onClick={handleLogoutClick}
          disabled={isLoggingOut}
          title="Sign Out"
          className={`p-1.5 text-slate-400 hover:text-rose-400 hover:bg-slate-800/80 rounded-xl border border-transparent hover:border-bordercolor transition cursor-pointer ${
            isLoggingOut ? 'opacity-60 cursor-not-allowed' : ''
          }`}
        >
          {isLoggingOut ? (
            <RotateCw className="w-4 h-4 animate-spin text-rose-400" />
          ) : (
            <LogOut className="w-4 h-4" />
          )}
        </button>
      </div>
    </header>
  );
};
