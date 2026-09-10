export interface ClientSummary {
  id: string;
  name: string;
  broker_client_id: string;
  status: 'ACTIVE' | 'PAUSED' | 'INACTIVE';
  trading_paused: boolean;
  container_status: string;
  execution_mode: 'LIVE' | 'PAPER';
  net_mtm: number;
  realized_pnl: number;
  unrealized_mtm: number;
  open_positions_count: number;
  orders_count: number;
  available_margin: number;
  margin_used: number;
  strategies_count: number;
  active_strategies_count: number;
  last_updated_time?: string;
  last_updated_timestamp?: number;
  webhook_url?: string;
}

export interface PositionItem {
  instrument_id: number;
  symbol: string;
  exchange_segment: string;
  product_type: string;
  quantity: number;
  buy_qty: number;
  sell_qty: number;
  buy_avg: number;
  sell_avg: number;
  ltp: number;
  pnl: number;
  realized_pnl: number;
  unrealized_pnl: number;
  lot_size?: number;
  multiplier?: number;
}

export interface OrderItem {
  app_order_id: string;
  order_id?: string;
  symbol: string;
  exchange_segment: string;
  side: 'BUY' | 'SELL';
  order_type: string;
  product_type: string;
  quantity: number;
  price: number;
  status: string;
  placed_at: string;
  status_message?: string;
  order_ref?: string;
  client_id?: string;
  client_name?: string;
}

export interface TradeItem {
  trade_id: string;
  app_order_id: string;
  symbol: string;
  exchange_segment: string;
  side: 'BUY' | 'SELL';
  quantity: number;
  price: number;
  executed_at: string;
}

export interface StrategyItem {
  id: string;
  tenant_id: string;
  symbol: string;
  exchange_segment: string;
  timeframe: string;
  quantity: number;
  product_type: string;
  atr_period: number;
  multiplier: number;
  execution_mode: 'LIVE' | 'PAPER';
  is_enabled: boolean;
  virtual_position: number;
  active_contract_id?: string;
  active_contract_desc?: string;
  current_trend?: 'BUY' | 'SELL' | 'FLAT' | 'NONE';
  last_eval_time?: string;
  next_poll_seconds?: number;
}

export interface MarginInfo {
  available_margin: number;
  margin_used: number;
  total_collateral: number;
  net_margin_available: number;
  cash_available: number;
  pay_in_amount: number;
  total_account_value: number;
  mcx_margin?: Record<string, number>;
  unified_margin?: Record<string, number>;
  shift_margin_needed?: boolean;
  shift_margin_message?: string;
  is_simulated?: boolean;
}

export interface CandleData {
  time: number; // Unix timestamp in seconds
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  supertrend?: number;
  trend_direction?: number; // 1 for UP/BUY (Green), -1 for DOWN/SELL (Red)
  upper_band?: number;
  lower_band?: number;
}

export interface ChartMarker {
  time: number;
  position: 'aboveBar' | 'belowBar';
  color: string;
  shape: 'arrowUp' | 'arrowDown' | 'circle';
  text: string;
}

export interface DashboardTelemetry {
  aggregate_net_mtm: number;
  aggregate_realized_pnl: number;
  aggregate_unrealized_mtm: number;
  total_clients_count: number;
  active_clients_count: number;
  paper_clients_count: number;
  live_clients_count: number;
  open_positions_total: number;
  market_status: {
    mcx_open: boolean;
    nse_open: boolean;
    current_time_ist: string;
  };
  clients: ClientSummary[];
}
