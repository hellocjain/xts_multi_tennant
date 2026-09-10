import React, { useState, useEffect } from 'react';
import { 
  ClientSummary, 
  PositionItem, 
  OrderItem, 
  TradeItem, 
  StrategyItem, 
  MarginInfo, 
  CandleData, 
  ChartMarker,
  ClientSettings
} from '../../types/telemetry';
import { api } from '../../services/api';
import { TradingViewChart } from '../trading/TradingViewChart';
import { StrategyRibbon } from '../trading/StrategyRibbon';
import { DeleteClientModal } from '../modals/DeleteClientModal';
import { ClientPanicModal } from '../modals/ClientPanicModal';
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
  Plus,
  Eye,
  EyeOff,
  Trash2,
  Lock,
  RefreshCw,
  XCircle,
  ShieldAlert,
  Key
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

  // Modals
  const [showPanicModal, setShowPanicModal] = useState(false);
  const [showDeleteModal, setShowDeleteModal] = useState(false);

  // Per-action debouncing & loading states
  const [squaringOffSymbol, setSquaringOffSymbol] = useState<string | null>(null);
  const [cancellingOrderId, setCancellingOrderId] = useState<string | null>(null);
  const [isTogglingTrading, setIsTogglingTrading] = useState(false);

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
  const [hasCopiedSecret, setHasCopiedSecret] = useState(false);

  // Settings state
  const [settings, setSettings] = useState<ClientSettings | null>(null);
  const [isLoadingSettings, setIsLoadingSettings] = useState(false);
  const [showApiKey, setShowApiKey] = useState(false);
  const [showApiSecret, setShowApiSecret] = useState(false);
  const [showWebhookSecret, setShowWebhookSecret] = useState(false);
  const [isSavingCreds, setIsSavingCreds] = useState(false);
  const [isSavingRisk, setIsSavingRisk] = useState(false);
  const [isRotatingSecret, setIsRotatingSecret] = useState(false);

  // Form states
  const [formName, setFormName] = useState('');
  const [formApiKey, setFormApiKey] = useState('');
  const [formApiSecret, setFormApiSecret] = useState('');
  const [formBrokerClientId, setFormBrokerClientId] = useState('');
  const [formExecMode, setFormExecMode] = useState<'LIVE' | 'PAPER'>('LIVE');

  const [formMaxLots, setFormMaxLots] = useState(100);
  const [formMaxOrderVal, setFormMaxOrderVal] = useState(5000000);
  const [formDailyNotional, setFormDailyNotional] = useState(10000000);
  const [formMaxDailyLoss, setFormMaxDailyLoss] = useState(50000);
  const [formSlippageBuf, setFormSlippageBuf] = useState(0.005);
  const [formMinDaysMcx, setFormMinDaysMcx] = useState(7);

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

  const loadSettings = async () => {
    setIsLoadingSettings(true);
    try {
      const data = await api.getClientSettings(clientId);
      setSettings(data);
      setFormName(data.name || '');
      setFormApiKey(data.credentials?.api_key || '');
      setFormApiSecret(data.credentials?.api_secret || '');
      setFormBrokerClientId(data.credentials?.broker_client_id || '');
      setFormExecMode(data.credentials?.execution_mode || 'LIVE');

      setFormMaxLots(data.risk_limits?.max_lots_limit || 100);
      setFormMaxOrderVal(data.risk_limits?.max_order_value_inr || 5000000);
      setFormDailyNotional(data.risk_limits?.daily_notional_cap_inr || 10000000);
      setFormMaxDailyLoss(data.risk_limits?.max_daily_loss_inr || 50000);
      setFormSlippageBuf(data.risk_limits?.slippage_buffer_pct || 0.005);
      setFormMinDaysMcx(data.risk_limits?.min_days_before_expiry_mcx || 7);
    } catch (err: any) {
      toast.error(`Failed to load client settings: ${err.message}`);
    } finally {
      setIsLoadingSettings(false);
    }
  };

  useEffect(() => {
    loadClientDetails();
    const interval = setInterval(loadClientDetails, 5000);
    return () => clearInterval(interval);
  }, [clientId]);

  useEffect(() => {
    if (activeSubTab === 'settings') {
      loadSettings();
    }
  }, [activeSubTab, clientId]);

  // Load chart data
  const loadChartData = async (symbol: string, tf: string, _stratId?: string) => {
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
    } catch {
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

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setShowAddStratModal(false);
        setShowDeleteModal(false);
        setShowPanicModal(false);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);

  const handleToggleTrading = async () => {
    if (!clientData || isTogglingTrading) return;
    const newPause = !clientData.client.trading_paused && clientData.client.status !== 'PAUSED';
    setIsTogglingTrading(true);
    try {
      await api.toggleClientTrading(clientId, newPause);
      toast.success(`Trading ${newPause ? 'PAUSED' : 'RESUMED'}`);
      loadClientDetails();
    } catch (err: any) {
      toast.error(`Error toggling trading: ${err.message}`);
    } finally {
      setIsTogglingTrading(false);
    }
  };

  const handleSquareOffPosition = async (pos: PositionItem) => {
    if (squaringOffSymbol) return;
    setSquaringOffSymbol(pos.symbol);
    toast.info(`Sending square-off order for ${pos.symbol}...`);
    try {
      await api.squareOffPosition(
        clientId,
        pos.symbol,
        Math.abs(pos.quantity),
        pos.quantity > 0 ? 'SELL' : 'BUY',
        pos.product_type
      );
      toast.success(`Square-off completed for ${pos.symbol}`);
      loadClientDetails();
    } catch (err: any) {
      toast.error(`Square-off failed: ${err.message}`);
    } finally {
      setSquaringOffSymbol(null);
    }
  };

  const handleCancelOrder = async (appOrderId: string) => {
    if (cancellingOrderId) return;
    setCancellingOrderId(appOrderId);
    toast.info(`Cancelling order ${appOrderId}...`);
    try {
      await api.cancelOrder(clientId, appOrderId);
      toast.success(`Order ${appOrderId} cancelled successfully`);
      loadClientDetails();
    } catch (err: any) {
      toast.error(`Cancel failed: ${err.message}`);
    } finally {
      setCancellingOrderId(null);
    }
  };

  const handleSaveCredentials = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSavingCreds(true);
    try {
      await api.updateClientCredentials(clientId, {
        name: formName,
        api_key: formApiKey,
        api_secret: formApiSecret,
        broker_client_id: formBrokerClientId,
        execution_mode: formExecMode,
      });
      toast.success('Credentials saved and container re-initialized');
      loadSettings();
      loadClientDetails();
    } catch (err: any) {
      toast.error(`Failed to save credentials: ${err.message}`);
    } finally {
      setIsSavingCreds(false);
    }
  };

  const handleSaveRiskLimits = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSavingRisk(true);
    try {
      await api.updateClientRiskLimits(clientId, {
        max_lots_limit: Number(formMaxLots),
        max_order_value_inr: Number(formMaxOrderVal),
        daily_notional_cap_inr: Number(formDailyNotional),
        max_daily_loss_inr: Number(formMaxDailyLoss),
        slippage_buffer_pct: Number(formSlippageBuf),
        min_days_before_expiry_mcx: Number(formMinDaysMcx),
      });
      toast.success('Risk parameters saved and configuration reloaded');
      loadSettings();
    } catch (err: any) {
      toast.error(`Failed to save risk limits: ${err.message}`);
    } finally {
      setIsSavingRisk(false);
    }
  };

  const handleRotateWebhookSecret = async () => {
    setIsRotatingSecret(true);
    try {
      await api.rotateWebhookSecret(clientId);
      toast.success('Webhook secret rotated successfully');
      loadSettings();
    } catch (err: any) {
      toast.error(`Failed to rotate secret: ${err.message}`);
    } finally {
      setIsRotatingSecret(false);
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
    try {
      await api.evaluateStrategyNow(clientId, strat.symbol, strat.timeframe, strat.id);
      loadChartData(strat.symbol, timeframe, strat.id);
    } catch (err: any) {
      toast.error(`Evaluation failed: ${err.message}`);
    }
  };

  const handleSyncTrend = async (strat: StrategyItem, trend: 'BUY' | 'SELL' | 'FLAT') => {
    try {
      await api.syncStrategyTrend(clientId, strat.symbol, strat.timeframe, trend, strat.id);
      loadClientDetails();
    } catch (err: any) {
      toast.error(`Sync trend failed: ${err.message}`);
    }
  };

  const handleResetFlat = async (strat: StrategyItem) => {
    try {
      await api.resetStrategyFlat(clientId, strat.symbol, strat.timeframe, strat.id);
      loadClientDetails();
    } catch (err: any) {
      toast.error(`Reset flat failed: ${err.message}`);
    }
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
                <h2 className="font-bold text-base text-slate-100">{client?.name || clientName || clientId}</h2>
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
              disabled={isTogglingTrading}
              onClick={handleToggleTrading}
              className={`px-3 py-1.5 rounded-xl font-semibold text-xs flex items-center space-x-1.5 border transition ${
                isPaused
                  ? 'bg-amber-500/20 text-amber-300 border-amber-500/40 hover:bg-amber-500/30'
                  : 'bg-slate-800 text-slate-200 border-slate-700 hover:bg-slate-750'
              } ${isTogglingTrading ? 'opacity-60 cursor-not-allowed' : ''}`}
            >
              {isTogglingTrading ? (
                <RotateCw className="w-3.5 h-3.5 animate-spin" />
              ) : isPaused ? (
                <Play className="w-3.5 h-3.5 fill-current" />
              ) : (
                <Pause className="w-3.5 h-3.5" />
              )}
              <span>{isTogglingTrading ? 'Updating...' : isPaused ? 'Resume Trading' : 'Pause Trading'}</span>
            </button>

            {/* Client Panic Button */}
            <button
              type="button"
              onClick={() => setShowPanicModal(true)}
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
            <ShieldAlert className="w-3.5 h-3.5" />
            <span>Risk & Credentials</span>
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
                    <th className="p-3 text-right">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-bordercolor/50 text-slate-200">
                  {(!clientData?.positions || clientData.positions.length === 0) ? (
                    <tr>
                      <td colSpan={8} className="p-8 text-center text-slate-500 italic">
                        No open market positions for this client.
                      </td>
                    </tr>
                  ) : (
                    clientData.positions.map((pos, idx) => {
                      const posProfit = (pos.pnl || 0) >= 0;
                      const isOpen = pos.quantity !== 0;
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
                          <td className="p-3 text-right">
                            {isOpen && (
                              <button
                                type="button"
                                disabled={squaringOffSymbol === pos.symbol}
                                onClick={() => handleSquareOffPosition(pos)}
                                className={`px-2.5 py-1 bg-rose-600/20 hover:bg-rose-600 text-rose-300 hover:text-white border border-rose-500/30 rounded-lg font-semibold text-[11px] transition flex items-center space-x-1 ml-auto ${
                                  squaringOffSymbol === pos.symbol ? 'opacity-60 cursor-not-allowed' : ''
                                }`}
                                title="Square off position immediately"
                              >
                                {squaringOffSymbol === pos.symbol ? (
                                  <RotateCw className="w-3.5 h-3.5 animate-spin" />
                                ) : (
                                  <XCircle className="w-3.5 h-3.5" />
                                )}
                                <span>{squaringOffSymbol === pos.symbol ? 'Exiting...' : 'Square Off'}</span>
                              </button>
                            )}
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
                    <th className="p-3 text-right">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-bordercolor/50 text-slate-200">
                  {(!clientData?.orders || clientData.orders.length === 0) ? (
                    <tr>
                      <td colSpan={9} className="p-8 text-center text-slate-500 italic">
                        No orders recorded for this client session.
                      </td>
                    </tr>
                  ) : (
                    clientData.orders.map((ord, idx) => {
                      const isOpen = ['OPEN', 'PENDING', 'TRIGGER_PENDING'].includes(ord.status.toUpperCase());
                      return (
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
                          <td className="p-3 text-right">
                            {isOpen && (
                              <button
                                type="button"
                                disabled={cancellingOrderId === ord.app_order_id}
                                onClick={() => handleCancelOrder(ord.app_order_id)}
                                className={`px-2.5 py-1 text-rose-400 hover:text-rose-300 hover:bg-rose-500/10 border border-rose-500/20 rounded font-semibold text-[10px] transition flex items-center space-x-1 ml-auto ${
                                  cancellingOrderId === ord.app_order_id ? 'opacity-60 cursor-not-allowed' : ''
                                }`}
                                title="Cancel Order"
                              >
                                {cancellingOrderId === ord.app_order_id ? (
                                  <RotateCw className="w-3 h-3 animate-spin" />
                                ) : null}
                                <span>{cancellingOrderId === ord.app_order_id ? 'Cancelling...' : 'Cancel'}</span>
                              </button>
                            )}
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

        {/* TAB 5: Risk & Credentials */}
        {activeSubTab === 'settings' && (
          <div className="space-y-6">
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              {/* CARD 1: Broker API Credentials */}
              <div className="bg-cardbg border border-bordercolor rounded-2xl p-5 space-y-4">
                <div className="flex items-center justify-between border-b border-bordercolor/60 pb-3">
                  <div className="flex items-center space-x-2 text-slate-100">
                    <Key className="w-4 h-4 text-brand-400" />
                    <h3 className="text-sm font-bold">Broker API Credentials</h3>
                  </div>
                  {isLoadingSettings && <RotateCw className="w-3.5 h-3.5 animate-spin text-brand-400" />}
                </div>

                <form onSubmit={handleSaveCredentials} className="space-y-3.5 text-xs font-mono">
                  <div className="space-y-1">
                    <label className="text-slate-400">Account Name</label>
                    <input
                      type="text"
                      required
                      value={formName}
                      onChange={(e) => setFormName(e.target.value)}
                      className="w-full px-3 py-2 rounded-xl bg-obsidian border border-bordercolor text-slate-100 focus:outline-none focus:border-brand-500"
                    />
                  </div>

                  <div className="space-y-1">
                    <label className="text-slate-400">Broker Client ID</label>
                    <input
                      type="text"
                      required
                      value={formBrokerClientId}
                      onChange={(e) => setFormBrokerClientId(e.target.value)}
                      className="w-full px-3 py-2 rounded-xl bg-obsidian border border-bordercolor text-slate-100 focus:outline-none focus:border-brand-500"
                    />
                  </div>

                  {/* API Key with Eye Toggle */}
                  <div className="space-y-1">
                    <label className="text-slate-400">Interactive API Key (App Key)</label>
                    <div className="relative">
                      <input
                        type={showApiKey ? 'text' : 'password'}
                        required
                        value={formApiKey}
                        onChange={(e) => setFormApiKey(e.target.value)}
                        className="w-full pl-3 pr-10 py-2 rounded-xl bg-obsidian border border-bordercolor text-slate-100 focus:outline-none focus:border-brand-500"
                      />
                      <button
                        type="button"
                        onClick={() => setShowApiKey(!showApiKey)}
                        className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-200"
                      >
                        {showApiKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                      </button>
                    </div>
                  </div>

                  {/* API Secret with Eye Toggle */}
                  <div className="space-y-1">
                    <label className="text-slate-400">Interactive Secret Key</label>
                    <div className="relative">
                      <input
                        type={showApiSecret ? 'text' : 'password'}
                        required
                        value={formApiSecret}
                        onChange={(e) => setFormApiSecret(e.target.value)}
                        className="w-full pl-3 pr-10 py-2 rounded-xl bg-obsidian border border-bordercolor text-slate-100 focus:outline-none focus:border-brand-500"
                      />
                      <button
                        type="button"
                        onClick={() => setShowApiSecret(!showApiSecret)}
                        className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-200"
                      >
                        {showApiSecret ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                      </button>
                    </div>
                  </div>

                  {/* Execution Mode Selector */}
                  <div className="space-y-1 pt-1">
                    <label className="text-slate-400">Execution Mode</label>
                    <div className="grid grid-cols-2 gap-2">
                      <button
                        type="button"
                        onClick={() => setFormExecMode('LIVE')}
                        className={`py-2 rounded-xl font-bold border transition ${
                          formExecMode === 'LIVE'
                            ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40'
                            : 'bg-obsidian text-slate-400 border-bordercolor hover:text-slate-200'
                        }`}
                      >
                        LIVE BROKER
                      </button>
                      <button
                        type="button"
                        onClick={() => setFormExecMode('PAPER')}
                        className={`py-2 rounded-xl font-bold border transition ${
                          formExecMode === 'PAPER'
                            ? 'bg-amber-500/20 text-amber-300 border-amber-500/40'
                            : 'bg-obsidian text-slate-400 border-bordercolor hover:text-slate-200'
                        }`}
                      >
                        PAPER SIMULATED
                      </button>
                    </div>
                  </div>

                  <div className="pt-2">
                    <button
                      type="submit"
                      disabled={isSavingCreds}
                      className="w-full py-2.5 bg-brand-600 hover:bg-brand-500 text-white font-bold rounded-xl flex items-center justify-center space-x-1.5 transition"
                    >
                      {isSavingCreds ? <RotateCw className="w-4 h-4 animate-spin" /> : <Lock className="w-4 h-4" />}
                      <span>Save Credentials & Re-Initialize</span>
                    </button>
                  </div>
                </form>
              </div>

              {/* CARD 2: Institutional Risk Limits */}
              <div className="bg-cardbg border border-bordercolor rounded-2xl p-5 space-y-4">
                <div className="flex items-center space-x-2 text-slate-100 border-b border-bordercolor/60 pb-3">
                  <ShieldAlert className="w-4 h-4 text-amber-400" />
                  <h3 className="text-sm font-bold">Capital & Risk Controls</h3>
                </div>

                <form onSubmit={handleSaveRiskLimits} className="space-y-3.5 text-xs font-mono">
                  <div className="grid grid-cols-2 gap-3">
                    <div className="space-y-1">
                      <label className="text-slate-400">Max Daily Loss (INR)</label>
                      <input
                        type="number"
                        required
                        min="500"
                        step="500"
                        value={formMaxDailyLoss}
                        onChange={(e) => setFormMaxDailyLoss(Number(e.target.value))}
                        className="w-full px-3 py-2 rounded-xl bg-obsidian border border-bordercolor text-slate-100 focus:outline-none focus:border-brand-500"
                      />
                    </div>

                    <div className="space-y-1">
                      <label className="text-slate-400">Daily Notional Cap (INR)</label>
                      <input
                        type="number"
                        required
                        min="1000"
                        step="10000"
                        value={formDailyNotional}
                        onChange={(e) => setFormDailyNotional(Number(e.target.value))}
                        className="w-full px-3 py-2 rounded-xl bg-obsidian border border-bordercolor text-slate-100 focus:outline-none focus:border-brand-500"
                      />
                    </div>
                  </div>

                  <div className="grid grid-cols-2 gap-3">
                    <div className="space-y-1">
                      <label className="text-slate-400">Max Lots Limit (Per Strategy)</label>
                      <input
                        type="number"
                        required
                        min="1"
                        value={formMaxLots}
                        onChange={(e) => setFormMaxLots(Number(e.target.value))}
                        className="w-full px-3 py-2 rounded-xl bg-obsidian border border-bordercolor text-slate-100 focus:outline-none focus:border-brand-500"
                      />
                    </div>

                    <div className="space-y-1">
                      <label className="text-slate-400">Max Single Order Value (INR)</label>
                      <input
                        type="number"
                        required
                        min="1000"
                        step="10000"
                        value={formMaxOrderVal}
                        onChange={(e) => setFormMaxOrderVal(Number(e.target.value))}
                        className="w-full px-3 py-2 rounded-xl bg-obsidian border border-bordercolor text-slate-100 focus:outline-none focus:border-brand-500"
                      />
                    </div>
                  </div>

                  <div className="grid grid-cols-2 gap-3">
                    <div className="space-y-1">
                      <label className="text-slate-400">Slippage Buffer Rate (%)</label>
                      <input
                        type="number"
                        required
                        min="0.01"
                        max="5"
                        step="0.05"
                        value={Math.round(formSlippageBuf * 10000) / 100}
                        onChange={(e) => setFormSlippageBuf(Number(e.target.value) / 100)}
                        className="w-full px-3 py-2 rounded-xl bg-obsidian border border-bordercolor text-slate-100 focus:outline-none focus:border-brand-500"
                      />
                    </div>

                    <div className="space-y-1">
                      <label className="text-slate-400">MCX Rollover Threshold (Days)</label>
                      <input
                        type="number"
                        required
                        min="1"
                        max="30"
                        value={formMinDaysMcx}
                        onChange={(e) => setFormMinDaysMcx(Number(e.target.value))}
                        className="w-full px-3 py-2 rounded-xl bg-obsidian border border-bordercolor text-slate-100 focus:outline-none focus:border-brand-500"
                      />
                    </div>
                  </div>

                  <div className="pt-2">
                    <button
                      type="submit"
                      disabled={isSavingRisk}
                      className="w-full py-2.5 bg-amber-600/90 hover:bg-amber-500 text-white font-bold rounded-xl flex items-center justify-center space-x-1.5 transition"
                    >
                      {isSavingRisk ? <RotateCw className="w-4 h-4 animate-spin" /> : <Sliders className="w-4 h-4" />}
                      <span>Save Live Risk Limits</span>
                    </button>
                  </div>
                </form>
              </div>
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              {/* CARD 3: Webhook Integration & Secret Rotator */}
              <div className="bg-cardbg border border-bordercolor rounded-2xl p-5 space-y-4">
                <div className="flex items-center space-x-2 text-slate-100 border-b border-bordercolor/60 pb-3">
                  <Sliders className="w-4 h-4 text-brand-400" />
                  <h3 className="text-sm font-bold">TradingView Webhook Integration</h3>
                </div>

                <div className="space-y-2">
                  <label className="text-xs font-mono text-slate-400">Webhook URL Endpoint</label>
                  <div className="flex items-center space-x-2">
                    <input
                      type="text"
                      readOnly
                      value={settings?.webhook?.webhook_url || client?.webhook_url || `${typeof window !== 'undefined' ? window.location.origin : ''}/webhook/${clientId}`}
                      className="flex-1 px-3 py-2 bg-obsidian border border-bordercolor rounded-xl text-xs font-mono text-slate-300 select-all"
                    />
                    <button
                      type="button"
                      onClick={() => {
                        const targetUrl = settings?.webhook?.webhook_url || client?.webhook_url || `${typeof window !== 'undefined' ? window.location.origin : ''}/webhook/${clientId}`;
                        navigator.clipboard.writeText(targetUrl);
                        setHasCopiedWebhook(true);
                        setTimeout(() => setHasCopiedWebhook(false), 2000);
                        toast.success('Webhook URL copied');
                      }}
                      className="p-2.5 bg-slate-800 hover:bg-slate-750 text-slate-200 rounded-xl transition"
                    >
                      {hasCopiedWebhook ? <Check className="w-4 h-4 text-emerald-400" /> : <Copy className="w-4 h-4" />}
                    </button>
                  </div>
                </div>

                <div className="space-y-2">
                  <label className="text-xs font-mono text-slate-400">Webhook Secret Key</label>
                  <div className="flex items-center space-x-2">
                    <input
                      type={showWebhookSecret ? 'text' : 'password'}
                      readOnly
                      value={settings?.webhook?.webhook_secret || '••••••••••••••••••••••••••••••••'}
                      className="flex-1 px-3 py-2 bg-obsidian border border-bordercolor rounded-xl text-xs font-mono text-slate-300"
                    />
                    <button
                      type="button"
                      onClick={() => setShowWebhookSecret(!showWebhookSecret)}
                      className="p-2.5 bg-slate-800 hover:bg-slate-750 text-slate-400 hover:text-slate-200 rounded-xl transition"
                      title={showWebhookSecret ? 'Hide secret' : 'Show secret'}
                    >
                      {showWebhookSecret ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        if (settings?.webhook?.webhook_secret) {
                          navigator.clipboard.writeText(settings.webhook.webhook_secret);
                          setHasCopiedSecret(true);
                          setTimeout(() => setHasCopiedSecret(false), 2000);
                          toast.success('Webhook secret copied');
                        }
                      }}
                      className="p-2.5 bg-slate-800 hover:bg-slate-750 text-slate-200 rounded-xl transition"
                      title="Copy Secret"
                    >
                      {hasCopiedSecret ? <Check className="w-4 h-4 text-emerald-400" /> : <Copy className="w-4 h-4" />}
                    </button>
                    <button
                      type="button"
                      onClick={handleRotateWebhookSecret}
                      disabled={isRotatingSecret}
                      className="px-3 py-2 bg-slate-800 hover:bg-slate-700 text-brand-400 rounded-xl border border-bordercolor font-semibold text-xs flex items-center space-x-1 transition"
                      title="Rotate Secret"
                    >
                      <RefreshCw className={`w-3.5 h-3.5 ${isRotatingSecret ? 'animate-spin' : ''}`} />
                      <span>Rotate</span>
                    </button>
                  </div>
                </div>

                <div className="space-y-2 pt-1">
                  <label className="text-xs font-mono text-slate-400">Sample TradingView Alert Message</label>
                  <pre className="p-3 bg-obsidian border border-bordercolor rounded-xl text-[11px] font-mono text-brand-300 overflow-x-auto">
{`{
  "secret": "${settings?.webhook?.webhook_secret || 'YOUR_SECRET'}",
  "action": "{{strategy.order.action}}",
  "symbol": "{{ticker}}",
  "quantity": {{strategy.order.contracts}},
  "price": {{close}}
}`}
                  </pre>
                </div>
              </div>

              {/* CARD 4: Danger Zone */}
              <div className="bg-cardbg border border-rose-500/30 rounded-2xl p-5 space-y-4 flex flex-col justify-between">
                <div className="space-y-3">
                  <div className="flex items-center space-x-2 text-rose-400 border-b border-rose-500/20 pb-3">
                    <AlertTriangle className="w-4 h-4" />
                    <h3 className="text-sm font-bold">Account Danger Zone</h3>
                  </div>

                  <p className="text-xs text-slate-300 leading-relaxed">
                    Terminates the client container instance, purges all associated SuperTrend algorithmic models, and deletes encrypted broker secrets.
                  </p>

                  <div className="p-3 bg-rose-950/20 border border-rose-500/20 rounded-xl text-xs text-rose-300/80 space-y-1 font-mono">
                    <div>• Container: xts_client_{clientId}</div>
                    <div>• Requires typing client ID to verify deletion</div>
                    <div>• Action cannot be reverted</div>
                  </div>
                </div>

                <div className="pt-4 border-t border-rose-500/20">
                  <button
                    type="button"
                    onClick={() => setShowDeleteModal(true)}
                    className="w-full py-2.5 bg-rose-600/20 hover:bg-rose-600 text-rose-300 hover:text-white border border-rose-500/30 rounded-xl font-bold text-xs flex items-center justify-center space-x-2 transition"
                  >
                    <Trash2 className="w-4 h-4" />
                    <span>Delete Client Account Permanently</span>
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Add Strategy Modal */}
      {showAddStratModal && (
        <div
          onClick={(e) => {
            if (e.target === e.currentTarget && !isSavingStrat) setShowAddStratModal(false);
          }}
          className="fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 flex items-center justify-center p-4"
        >
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
                {/* 1-Click Symbol Presets */}
                <div className="flex flex-wrap gap-1.5 pt-1">
                  {['CRUDEOIL1!', 'GOLD1!', 'GOLDPETAL1!', 'SILVER1!', 'SILVER100', 'NATURALGAS1!', 'COPPER1!'].map((sym) => (
                    <button
                      key={sym}
                      type="button"
                      onClick={() => setNewStratSymbol(sym)}
                      className={`px-2 py-0.5 rounded text-[10px] font-mono border transition cursor-pointer ${
                        newStratSymbol === sym
                          ? 'bg-brand-600/30 border-brand-500 text-brand-300 font-bold'
                          : 'bg-obsidian border-slate-800 text-slate-400 hover:text-slate-200 hover:border-slate-700'
                      }`}
                    >
                      {sym}
                    </button>
                  ))}
                </div>
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

      {/* Emergency Client Panic Modal */}
      {showPanicModal && (
        <ClientPanicModal
          isOpen={showPanicModal}
          clientId={clientId}
          clientName={client?.name || clientId}
          onClose={() => setShowPanicModal(false)}
          onSuccess={() => {
            setShowPanicModal(false);
            loadClientDetails();
          }}
        />
      )}

      {/* 2-Step Verification Delete Client Modal */}
      {showDeleteModal && (
        <DeleteClientModal
          isOpen={showDeleteModal}
          clientId={clientId}
          clientName={client?.name || clientId}
          onClose={() => setShowDeleteModal(false)}
          onSuccess={() => {
            setShowDeleteModal(false);
            if (onCloseTab) onCloseTab();
          }}
        />
      )}
    </div>
  );
};
