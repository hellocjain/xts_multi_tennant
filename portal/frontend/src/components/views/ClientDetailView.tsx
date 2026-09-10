import React, { useState, useEffect } from 'react';
import { 
  ClientSummary, 
  PositionItem, 
  OrderItem, 
  TradeItem, 
  StrategyItem, 
  MarginInfo, 
  CandleData, 
  ChartMarker 
} from '../../types/telemetry';
import { api } from '../../services/api';
import { TradingViewChart } from '../trading/TradingViewChart';
import { StrategyRibbon } from '../trading/StrategyRibbon';
import { 
  Layers, 
  LineChart, 
  BookOpen, 
  CheckCircle2, 
  Sliders, 
  Flame, 
  Pause, 
  Play, 
  RotateCw, 
  Copy, 
  Check, 
  AlertTriangle,
  X,
  Plus
} from 'lucide-react';
import { toast } from 'sonner';

interface ClientDetailViewProps {
  clientId: string;
  clientName?: string;
  onCloseTab?: () => void;
}

export const ClientDetailView: React.FC<ClientDetailViewProps> = ({
  clientId,
  clientName,
  onCloseTab,
}) => {
  const [activeSubTab, setActiveSubTab] = useState<'chart' | 'positions' | 'orders' | 'trades' | 'settings'>('chart');
  const [isLoading, setIsLoading] = useState(true);
  const [clientData, setClientData] = useState<{
    client: ClientSummary;
    positions: PositionItem[];
    orders: OrderItem[];
    trades: TradeItem[];
    strategies: StrategyItem[];
    margin: MarginInfo;
  } | null>(null);

  // Charting state
  const [selectedStrategy, setSelectedStrategy] = useState<StrategyItem | null>(null);
  const [timeframe, setTimeframe] = useState<string>('5m');
  const [candleData, setCandleData] = useState<{
    candles: CandleData[];
    supertrend_line: Array<{ time: number; value: number; color: string }>;
    upper_band?: Array<{ time: number; value: number }>;
    lower_band?: Array<{ time: number; value: number }>;
    markers: ChartMarker[];
  }>({
    candles: [],
    supertrend_line: [],
    markers: [],
  });
  const [isChartLoading, setIsChartLoading] = useState(false);

  // Add Strategy Modal
  const [showAddStratModal, setShowAddStratModal] = useState(false);
  const [newStratSymbol, setNewStratSymbol] = useState('CRUDEOIL1!');
  const [newStratTf, setNewStratTf] = useState('5m');
  const [newStratQty, setNewStratQty] = useState(1);
  const [newStratAtr, setNewStratAtr] = useState(7);
  const [newStratMult, setNewStratMult] = useState(3.0);
  const [isSavingStrat, setIsSavingStrat] = useState(false);

  // Copied state
  const [hasCopiedWebhook, setHasCopiedWebhook] = useState(false);

  // Load client detail data
  const loadClientDetails = async () => {
    try {
      const data = await api.getClientDetail(clientId);
      setClientData(data);
      if (data.strategies && data.strategies.length > 0 && !selectedStrategy) {
        setSelectedStrategy(data.strategies[0]);
        setTimeframe(data.strategies[0].timeframe || '5m');
      }
    } catch (err: any) {
      toast.error(`Failed to load client details: ${err.message}`);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    loadClientDetails();
    const interval = setInterval(loadClientDetails, 5000);
    return () => clearInterval(interval);
  }, [clientId]);

  // Load chart data
  const loadChartData = async (symbol: string, tf: string, stratId?: string) => {
    if (!symbol) return;
    setIsChartLoading(true);
    try {
      const res = await api.getCandles(clientId, symbol, tf);
      setCandleData({
        candles: res.candles || [],
        supertrend_line: res.supertrend_line || [],
        upper_band: res.upper_band || [],
        lower_band: res.lower_band || [],
        markers: res.markers || [],
      });
    } catch (err: any) {
      // chart error non-blocking
    } finally {
      setIsChartLoading(false);
    }
  };

  useEffect(() => {
    if (selectedStrategy) {
      loadChartData(selectedStrategy.symbol, timeframe, selectedStrategy.id);
    }
  }, [selectedStrategy, timeframe]);

  const handleToggleTrading = async () => {
    if (!clientData) return;
    const newPause = !clientData.client.trading_paused && clientData.client.status !== 'PAUSED';
    try {
      await api.toggleClientTrading(clientId, newPause);
      toast.success(`Trading ${newPause ? 'PAUSED' : 'RESUMED'}`);
      loadClientDetails();
    } catch (err: any) {
      toast.error(`Error toggling trading: ${err.message}`);
    }
  };

  const handlePanicClient = async () => {
    if (!confirm(`🚨 PANIC: Immediately square off all positions and cancel orders for ${clientName || clientId}?`)) return;
    try {
      await api.panicClient(clientId);
      toast.success(`Panic square-off completed for ${clientName || clientId}`);
      loadClientDetails();
    } catch (err: any) {
      toast.error(`Panic failed: ${err.message}`);
    }
  };

  const handleToggleStrategy = async (stratId: string) => {
    try {
      await api.toggleStrategy(clientId, stratId);
      loadClientDetails();
    } catch (err: any) {
      toast.error(`Failed to toggle strategy: ${err.message}`);
    }
  };

  const handleEvaluateNow = async (strat: StrategyItem) => {
    await api.evaluateStrategyNow(clientId, strat.symbol, strat.timeframe, strat.id);
    loadChartData(strat.symbol, timeframe, strat.id);
  };

  const handleSyncTrend = async (strat: StrategyItem, trend: 'BUY' | 'SELL' | 'FLAT') => {
    await api.syncStrategyTrend(clientId, strat.symbol, strat.timeframe, trend, strat.id);
    loadClientDetails();
  };

  const handleResetFlat = async (strat: StrategyItem) => {
    await api.resetStrategyFlat(clientId, strat.symbol, strat.timeframe, strat.id);
    loadClientDetails();
  };

  const handleDeleteStrategy = async (stratId: string) => {
    try {
      await api.deleteStrategy(clientId, stratId);
      toast.success('Strategy removed');
      setSelectedStrategy(null);
      loadClientDetails();
    } catch (err: any) {
      toast.error(`Delete failed: ${err.message}`);
    }
  };

  const handleAddStrategy = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSavingStrat(true);
    try {
      await api.saveStrategy(clientId, {
        symbol: newStratSymbol.trim().toUpperCase(),
        timeframe: newStratTf,
        quantity: Number(newStratQty),
        atr_period: Number(newStratAtr),
        multiplier: Number(newStratMult),
        product_type: 'MIS',
        execution_mode: clientData?.client?.execution_mode || 'LIVE',
      });
      toast.success(`Strategy ${newStratSymbol} added successfully`);
      setShowAddStratModal(false);
      loadClientDetails();
    } catch (err: any) {
      toast.error(`Failed to add strategy: ${err.message}`);
    } finally {
      setIsSavingStrat(false);
    }
  };

  const formatINR = (val: number = 0) => {
    return `₹${val.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  };

  if (isLoading && !clientData) {
    return (
      <div className="flex-1 flex items-center justify-center p-8">
        <div className="flex items-center space-x-2 text-xs font-mono text-brand-400">
          <RotateCw className="w-4 h-4 animate-spin" />
          <span>Loading client workspace for {clientId}...</span>
        </div>
      </div>
    );
  }

  const client = clientData?.client;
  const isProfit = (client?.net_mtm || 0) >= 0;
  const isPaused = client?.trading_paused || client?.status === 'PAUSED';

  return (
    <div className="flex-1 flex flex-col overflow-hidden bg-obsidian">
      {/* Client Header Bar */}
      <div className="bg-cardbg border-b border-bordercolor px-4 py-3 shrink-0">
        <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
          {/* Client Title & Identity */}
          <div className="flex items-center space-x-3">
            <div className="w-10 h-10 rounded-xl bg-slate-800 border border-bordercolor flex items-center justify-center text-slate-100 font-mono font-bold text-sm">
              {clientId.substring(0, 3).toUpperCase()}
            </div>
            <div>
              <div className="flex items-center space-x-2">
                <h2 className="font-bold text-base text-slate-100">{client?.name || clientId}</h2>
                <span className="text-[11px] font-mono px-2 py-0.5 rounded bg-slate-800 text-slate-400 border border-slate-700">
                  {clientId}
                </span>
                <span
                  className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded-full uppercase ${
                    client?.execution_mode === 'LIVE'
                      ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
                      : 'bg-amber-500/10 text-amber-400 border border-amber-500/20'
                  }`}
                >
                  {client?.execution_mode || 'LIVE'}
                </span>
              </div>
              <div className="text-xs font-mono text-slate-400 mt-0.5">
                Broker Client ID: <span className="text-slate-300 font-semibold">{client?.broker_client_id || 'N/A'}</span>
              </div>
            </div>
          </div>

          {/* Key Financial KPIs & Actions */}
          <div className="flex flex-wrap items-center gap-3">
            {/* Net MTM Pill */}
            <div className="bg-obsidian rounded-xl px-3 py-1.5 border border-bordercolor">
              <div className="text-[10px] font-semibold text-slate-400 uppercase tracking-wider">NET MTM</div>
              <div className={`font-mono text-sm font-bold ${isProfit ? 'text-emerald-400' : 'text-rose-400'}`}>
                {isProfit ? '+' : ''}{formatINR(client?.net_mtm)}
              </div>
            </div>

            {/* Margin Pill */}
            <div className="bg-obsidian rounded-xl px-3 py-1.5 border border-bordercolor">
              <div className="text-[10px] font-semibold text-slate-400 uppercase tracking-wider">AVAIL MARGIN</div>
              <div className="font-mono text-sm font-bold text-slate-200">
                {formatINR(clientData?.margin?.available_margin || client?.available_margin)}
              </div>
            </div>

            {/* Trading Toggle */}
            <button
              type="button"
              onClick={handleToggleTrading}
              className={`px-3 py-1.5 rounded-xl font-semibold text-xs flex items-center space-x-1.5 border transition ${
                isPaused
                  ? 'bg-amber-500/20 text-amber-300 border-amber-500/40 hover:bg-amber-500/30'
                  : 'bg-slate-800 text-slate-200 border-slate-700 hover:bg-slate-750'
              }`}
            >
              {isPaused ? <Play className="w-3.5 h-3.5 fill-current" /> : <Pause className="w-3.5 h-3.5" />}
              <span>{isPaused ? 'Resume Trading' : 'Pause Trading'}</span>
            </button>

            {/* Client Panic Button */}
            <button
              type="button"
              onClick={handlePanicClient}
              className="px-3 py-1.5 bg-rose-600/20 hover:bg-rose-600 text-rose-300 hover:text-white border border-rose-500/30 rounded-xl font-semibold text-xs flex items-center space-x-1.5 transition"
            >
              <Flame className="w-3.5 h-3.5" />
              <span>Panic Client</span>
            </button>
          </div>
        </div>

        {/* Sub-Tab Navigation Bar */}
        <div className="flex items-center space-x-2 mt-4 pt-2 border-t border-bordercolor/60 overflow-x-auto no-scrollbar text-xs">
          <button
            type="button"
            onClick={() => setActiveSubTab('chart')}
            className={`flex items-center space-x-1.5 px-3 py-1.5 rounded-lg font-medium transition ${
              activeSubTab === 'chart'
                ? 'bg-brand-600 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800'
            }`}
          >
            <LineChart className="w-3.5 h-3.5" />
            <span>SuperTrend & Chart</span>
          </button>

          <button
            type="button"
            onClick={() => setActiveSubTab('positions')}
            className={`flex items-center space-x-1.5 px-3 py-1.5 rounded-lg font-medium transition ${
              activeSubTab === 'positions'
                ? 'bg-brand-600 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800'
            }`}
          >
            <Layers className="w-3.5 h-3.5" />
            <span>Open Positions ({clientData?.positions?.length || 0})</span>
          </button>

          <button
            type="button"
            onClick={() => setActiveSubTab('orders')}
            className={`flex items-center space-x-1.5 px-3 py-1.5 rounded-lg font-medium transition ${
              activeSubTab === 'orders'
                ? 'bg-brand-600 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800'
            }`}
          >
            <BookOpen className="w-3.5 h-3.5" />
            <span>Orders ({clientData?.orders?.length || 0})</span>
          </button>

          <button
            type="button"
            onClick={() => setActiveSubTab('trades')}
            className={`flex items-center space-x-1.5 px-3 py-1.5 rounded-lg font-medium transition ${
              activeSubTab === 'trades'
                ? 'bg-brand-600 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800'
            }`}
          >
            <CheckCircle2 className="w-3.5 h-3.5" />
            <span>Trades / Fills</span>
          </button>

          <button
            type="button"
            onClick={() => setActiveSubTab('settings')}
            className={`flex items-center space-x-1.5 px-3 py-1.5 rounded-lg font-medium transition ${
              activeSubTab === 'settings'
                ? 'bg-brand-600 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800'
            }`}
          >
            <Sliders className="w-3.5 h-3.5" />
            <span>Credentials & Webhook</span>
          </button>
        </div>
      </div>

      {/* Sub-Tab Contents Area */}
      <div className="flex-1 overflow-y-auto p-4 md:p-6 space-y-4">
        {/* TAB 1: SuperTrend & Charting */}
        {activeSubTab === 'chart' && (
          <div className="space-y-4">
            <StrategyRibbon
              strategies={clientData?.strategies || []}
              selectedStrategyId={selectedStrategy?.id}
              onSelectStrategy={(strat) => {
                setSelectedStrategy(strat);
                setTimeframe(strat.timeframe || '5m');
              }}
              onToggleStrategy={handleToggleStrategy}
              onEvaluateNow={handleEvaluateNow}
              onSyncTrend={handleSyncTrend}
              onResetFlat={handleResetFlat}
              onDeleteStrategy={handleDeleteStrategy}
              onAddStrategyClick={() => setShowAddStratModal(true)}
            />

            <TradingViewChart
              candles={candleData.candles}
              supertrendLine={candleData.supertrend_line}
              upperBand={candleData.upper_band}
              lowerBand={candleData.lower_band}
              markers={candleData.markers}
              symbol={selectedStrategy?.symbol || 'CRUDEOIL1!'}
              timeframe={timeframe}
              onChangeTimeframe={(tf) => setTimeframe(tf)}
              onRefresh={() => {
                if (selectedStrategy) loadChartData(selectedStrategy.symbol, timeframe, selectedStrategy.id);
              }}
              isLoading={isChartLoading}
            />
          </div>
        )}

        {/* TAB 2: Positions Table */}
        {activeSubTab === 'positions' && (
          <div className="bg-cardbg border border-bordercolor rounded-2xl overflow-hidden shadow-sm">
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs font-mono">
                <thead className="bg-slate-950/80 text-slate-400 text-[11px] uppercase tracking-wider border-b border-bordercolor select-none">
                  <tr>
                    <th className="p-3">Symbol</th>
                    <th className="p-3">Product</th>
                    <th className="p-3 text-right">Net Qty</th>
                    <th className="p-3 text-right">Buy Avg</th>
                    <th className="p-3 text-right">Sell Avg</th>
                    <th className="p-3 text-right">LTP</th>
                    <th className="p-3 text-right">PnL</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-bordercolor/50 text-slate-200">
                  {(!clientData?.positions || clientData.positions.length === 0) ? (
                    <tr>
                      <td colSpan={7} className="p-8 text-center text-slate-500 italic">
                        No open market positions for this client.
                      </td>
                    </tr>
                  ) : (
                    clientData.positions.map((pos, idx) => {
                      const posProfit = (pos.pnl || 0) >= 0;
                      return (
                        <tr key={idx} className="hover:bg-slate-800/40 transition">
                          <td className="p-3 font-bold text-slate-100">{pos.symbol}</td>
                          <td className="p-3 text-slate-400">{pos.product_type || 'MIS'}</td>
                          <td className={`p-3 text-right font-bold ${pos.quantity > 0 ? 'text-emerald-400' : pos.quantity < 0 ? 'text-rose-400' : 'text-slate-400'}`}>
                            {pos.quantity > 0 ? `+${pos.quantity}` : pos.quantity}
                          </td>
                          <td className="p-3 text-right text-slate-300">{pos.buy_avg || '0.00'}</td>
                          <td className="p-3 text-right text-slate-300">{pos.sell_avg || '0.00'}</td>
                          <td className="p-3 text-right font-bold text-slate-100">{pos.ltp || '0.00'}</td>
                          <td className={`p-3 text-right font-bold ${posProfit ? 'text-emerald-400' : 'text-rose-400'}`}>
                            {posProfit ? '+' : ''}{formatINR(pos.pnl)}
                          </td>
                        </tr>
                      );
                    })
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* TAB 3: Orders Table */}
        {activeSubTab === 'orders' && (
          <div className="bg-cardbg border border-bordercolor rounded-2xl overflow-hidden shadow-sm">
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs font-mono">
                <thead className="bg-slate-950/80 text-slate-400 text-[11px] uppercase tracking-wider border-b border-bordercolor select-none">
                  <tr>
                    <th className="p-3">App Order ID</th>
                    <th className="p-3">Time</th>
                    <th className="p-3">Symbol</th>
                    <th className="p-3">Side</th>
                    <th className="p-3 text-right">Qty</th>
                    <th className="p-3 text-right">Price</th>
                    <th className="p-3">Status</th>
                    <th className="p-3">Message</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-bordercolor/50 text-slate-200">
                  {(!clientData?.orders || clientData.orders.length === 0) ? (
                    <tr>
                      <td colSpan={8} className="p-8 text-center text-slate-500 italic">
                        No orders recorded for this client session.
                      </td>
                    </tr>
                  ) : (
                    clientData.orders.map((ord, idx) => (
                      <tr key={idx} className="hover:bg-slate-800/40 transition">
                        <td className="p-3 font-semibold text-slate-300">{ord.app_order_id}</td>
                        <td className="p-3 text-slate-400">{ord.placed_at || '--:--:--'}</td>
                        <td className="p-3 font-bold text-slate-100">{ord.symbol}</td>
                        <td className="p-3">
                          <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                            ord.side === 'BUY' ? 'bg-emerald-500/20 text-emerald-400' : 'bg-rose-500/20 text-rose-400'
                          }`}>
                            {ord.side}
                          </span>
                        </td>
                        <td className="p-3 text-right text-slate-100">{ord.quantity}</td>
                        <td className="p-3 text-right text-slate-300">{ord.price || 'MARKET'}</td>
                        <td className="p-3">
                          <span className={`px-2 py-0.5 rounded text-[10px] font-semibold ${
                            ord.status === 'COMPLETE'
                              ? 'bg-emerald-500/10 text-emerald-400'
                              : ord.status === 'REJECTED'
                              ? 'bg-rose-500/10 text-rose-400'
                              : 'bg-amber-500/10 text-amber-400'
                          }`}>
                            {ord.status}
                          </span>
                        </td>
                        <td className="p-3 text-slate-400 truncate max-w-xs">{ord.status_message || '-'}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* TAB 4: Trades Table */}
        {activeSubTab === 'trades' && (
          <div className="bg-cardbg border border-bordercolor rounded-2xl overflow-hidden shadow-sm">
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs font-mono">
                <thead className="bg-slate-950/80 text-slate-400 text-[11px] uppercase tracking-wider border-b border-bordercolor select-none">
                  <tr>
                    <th className="p-3">Trade ID</th>
                    <th className="p-3">Order ID</th>
                    <th className="p-3">Time</th>
                    <th className="p-3">Symbol</th>
                    <th className="p-3">Side</th>
                    <th className="p-3 text-right">Qty</th>
                    <th className="p-3 text-right">Executed Price</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-bordercolor/50 text-slate-200">
                  {(!clientData?.trades || clientData.trades.length === 0) ? (
                    <tr>
                      <td colSpan={7} className="p-8 text-center text-slate-500 italic">
                        No trade fills recorded today.
                      </td>
                    </tr>
                  ) : (
                    clientData.trades.map((trd, idx) => (
                      <tr key={idx} className="hover:bg-slate-800/40 transition">
                        <td className="p-3 font-semibold text-slate-300">{trd.trade_id}</td>
                        <td className="p-3 text-slate-400">{trd.app_order_id}</td>
                        <td className="p-3 text-slate-400">{trd.executed_at}</td>
                        <td className="p-3 font-bold text-slate-100">{trd.symbol}</td>
                        <td className="p-3">
                          <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                            trd.side === 'BUY' ? 'bg-emerald-500/20 text-emerald-400' : 'bg-rose-500/20 text-rose-400'
                          }`}>
                            {trd.side}
                          </span>
                        </td>
                        <td className="p-3 text-right text-slate-100">{trd.quantity}</td>
                        <td className="p-3 text-right font-bold text-slate-100">{trd.price}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* TAB 5: Credentials & Webhook Settings */}
        {activeSubTab === 'settings' && (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* Webhook Configuration Box */}
            <div className="bg-cardbg border border-bordercolor rounded-2xl p-5 space-y-4">
              <h3 className="text-sm font-bold text-slate-100 flex items-center space-x-2">
                <span>TradingView Webhook Integration</span>
              </h3>
              <p className="text-xs text-slate-400 leading-relaxed">
                Connect external TradingView alert triggers to route execution directly to this client container.
              </p>

              <div className="space-y-2">
                <label className="text-xs font-mono text-slate-400">Webhook URL Endpoint</label>
                <div className="flex items-center space-x-2">
                  <input
                    type="text"
                    readOnly
                    value={client?.webhook_url || `http://139.59.20.239/webhook/${clientId}`}
                    className="flex-1 px-3 py-2 bg-obsidian border border-bordercolor rounded-xl text-xs font-mono text-slate-300 select-all"
                  />
                  <button
                    type="button"
                    onClick={() => {
                      navigator.clipboard.writeText(client?.webhook_url || `http://139.59.20.239/webhook/${clientId}`);
                      setHasCopiedWebhook(true);
                      setTimeout(() => setHasCopiedWebhook(false), 2000);
                      toast.success('Webhook URL copied to clipboard');
                    }}
                    className="p-2.5 bg-slate-800 hover:bg-slate-700 text-slate-200 rounded-xl transition"
                  >
                    {hasCopiedWebhook ? <Check className="w-4 h-4 text-emerald-400" /> : <Copy className="w-4 h-4" />}
                  </button>
                </div>
              </div>

              <div className="space-y-2 pt-2">
                <label className="text-xs font-mono text-slate-400">Sample Alert Message Payload (JSON)</label>
                <pre className="p-3 bg-obsidian border border-bordercolor rounded-xl text-[11px] font-mono text-brand-300 overflow-x-auto">
{`{
  "secret": "YOUR_WEBHOOK_SECRET",
  "action": "{{strategy.order.action}}",
  "symbol": "{{ticker}}",
  "quantity": {{strategy.order.contracts}},
  "price": {{close}}
}`}
                </pre>
              </div>
            </div>

            {/* Account Credentials Info */}
            <div className="bg-cardbg border border-bordercolor rounded-2xl p-5 space-y-4">
              <h3 className="text-sm font-bold text-slate-100">Broker Account Details</h3>
              <div className="space-y-3 text-xs font-mono text-slate-300">
                <div className="flex justify-between py-2 border-b border-bordercolor/60">
                  <span className="text-slate-400">Tenant Container:</span>
                  <span className="font-semibold text-slate-100">{clientId}</span>
                </div>
                <div className="flex justify-between py-2 border-b border-bordercolor/60">
                  <span className="text-slate-400">Broker Client ID:</span>
                  <span className="font-semibold text-slate-100">{client?.broker_client_id}</span>
                </div>
                <div className="flex justify-between py-2 border-b border-bordercolor/60">
                  <span className="text-slate-400">Execution Mode:</span>
                  <span className={`font-bold ${client?.execution_mode === 'LIVE' ? 'text-emerald-400' : 'text-amber-400'}`}>
                    {client?.execution_mode}
                  </span>
                </div>
                <div className="flex justify-between py-2 border-b border-bordercolor/60">
                  <span className="text-slate-400">Total Collateral:</span>
                  <span>{formatINR(clientData?.margin?.total_collateral)}</span>
                </div>
                <div className="flex justify-between py-2">
                  <span className="text-slate-400">Account Value:</span>
                  <span>{formatINR(clientData?.margin?.total_account_value)}</span>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Add Strategy Modal */}
      {showAddStratModal && (
        <div className="fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className="bg-cardbg border border-bordercolor w-full max-w-md rounded-2xl p-6 shadow-2xl space-y-4 animate-in fade-in zoom-in-95 duration-150">
            <div className="flex items-center justify-between">
              <h3 className="font-bold text-base text-slate-100">Add SuperTrend Strategy</h3>
              <button
                type="button"
                onClick={() => setShowAddStratModal(false)}
                className="text-slate-400 hover:text-slate-200 p-1 rounded-lg"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <form onSubmit={handleAddStrategy} className="space-y-3.5 text-xs font-mono">
              <div className="space-y-1">
                <label className="text-slate-400">Symbol (e.g. CRUDEOIL1!, GOLDPETAL1!, SILVER100)</label>
                <input
                  type="text"
                  required
                  value={newStratSymbol}
                  onChange={(e) => setNewStratSymbol(e.target.value.toUpperCase())}
                  className="w-full px-3 py-2 rounded-xl bg-obsidian border border-bordercolor text-slate-100 focus:outline-none focus:border-brand-500"
                />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1">
                  <label className="text-slate-400">Timeframe</label>
                  <select
                    value={newStratTf}
                    onChange={(e) => setNewStratTf(e.target.value)}
                    className="w-full px-3 py-2 rounded-xl bg-obsidian border border-bordercolor text-slate-100 focus:outline-none focus:border-brand-500"
                  >
                    <option value="1m">1m</option>
                    <option value="3m">3m</option>
                    <option value="5m">5m</option>
                    <option value="15m">15m</option>
                    <option value="20m">20m</option>
                    <option value="30m">30m</option>
                    <option value="1h">1h</option>
                  </select>
                </div>

                <div className="space-y-1">
                  <label className="text-slate-400">Lots / Contracts</label>
                  <input
                    type="number"
                    min="1"
                    required
                    value={newStratQty}
                    onChange={(e) => setNewStratQty(Number(e.target.value))}
                    className="w-full px-3 py-2 rounded-xl bg-obsidian border border-bordercolor text-slate-100 focus:outline-none focus:border-brand-500"
                  />
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1">
                  <label className="text-slate-400">ATR Period</label>
                  <input
                    type="number"
                    min="1"
                    required
                    value={newStratAtr}
                    onChange={(e) => setNewStratAtr(Number(e.target.value))}
                    className="w-full px-3 py-2 rounded-xl bg-obsidian border border-bordercolor text-slate-100 focus:outline-none focus:border-brand-500"
                  />
                </div>

                <div className="space-y-1">
                  <label className="text-slate-400">Multiplier</label>
                  <input
                    type="number"
                    step="0.1"
                    min="0.1"
                    required
                    value={newStratMult}
                    onChange={(e) => setNewStratMult(Number(e.target.value))}
                    className="w-full px-3 py-2 rounded-xl bg-obsidian border border-bordercolor text-slate-100 focus:outline-none focus:border-brand-500"
                  />
                </div>
              </div>

              <div className="flex items-center justify-end space-x-2 pt-3 border-t border-bordercolor">
                <button
                  type="button"
                  onClick={() => setShowAddStratModal(false)}
                  className="px-4 py-2 rounded-xl text-slate-400 hover:text-slate-200 bg-slate-800"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={isSavingStrat}
                  className="px-4 py-2 rounded-xl bg-brand-600 hover:bg-brand-500 text-white font-semibold flex items-center space-x-1"
                >
                  {isSavingStrat ? <RotateCw className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
                  <span>Save Strategy</span>
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
};
