import { DashboardTelemetry, ClientSummary, PositionItem, OrderItem, TradeItem, StrategyItem, MarginInfo, CandleData, ChartMarker, ClientSettings } from '../types/telemetry';

const BASE_URL = '';

export class ApiError extends Error {
  constructor(public status: number, message: string, public data?: any) {
    super(message);
    this.name = 'ApiError';
  }
}

async function request<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers || {});
  if (!headers.has('Content-Type') && !(options.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json');
  }
  headers.set('Accept', 'application/json');
  headers.set('X-Requested-With', 'XMLHttpRequest');

  const response = await fetch(`${BASE_URL}${endpoint}`, {
    ...options,
    headers,
    credentials: 'include', // Automatically attaches secure xts_session cookie
  });

  if (response.status === 401) {
    // Session expired or unauthenticated
    if (!endpoint.includes('/api/auth/me') && !endpoint.includes('/api/auth/login')) {
      window.dispatchEvent(new CustomEvent('auth:unauthorized'));
    }
  }

  let data: any = null;
  const contentType = response.headers.get('content-type') || '';
  if (contentType.includes('application/json')) {
    data = await response.json().catch(() => null);
  } else {
    data = await response.text().catch(() => null);
  }

  if (!response.ok) {
    const errorMsg = data?.detail || data?.message || `HTTP ${response.status}: ${response.statusText}`;
    throw new ApiError(response.status, errorMsg, data);
  }

  return data as T;
}

export const api = {
  // Auth
  getMe: () => request<{ authenticated: boolean; user?: { username: string } }>('/api/auth/me'),
  login: (username: string, password: string, totp_code?: string) =>
    request<{ status: string; user?: any }>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password, totp_code }),
    }),
  logout: () => request<{ status: string }>('/api/auth/logout', { method: 'POST' }),

  // Dashboard
  getDashboardData: () => request<DashboardTelemetry>('/api/dashboard'),
  panicAll: () => request<{ status: string; result: any }>('/admin/panic-all', { method: 'POST' }),

  // Client Deep Dive
  getClientDetail: (clientId: string) =>
    request<{
      client: ClientSummary;
      positions: PositionItem[];
      orders: OrderItem[];
      trades: TradeItem[];
      strategies: StrategyItem[];
      margin: MarginInfo;
    }>(`/api/clients/${clientId}`),

  toggleClientTrading: (clientId: string, pause: boolean) =>
    request<{ status: string; trading_paused: boolean }>(`/admin/clients/${clientId}/toggle-trading`, {
      method: 'POST',
      body: JSON.stringify({ pause }),
    }),

  panicClient: (clientId: string) =>
    request<{ status: string; result: any }>(`/admin/clients/${clientId}/panic`, { method: 'POST' }),

  addClient: (payload: any) =>
    request<{ status: string; client_id: string }>('/api/clients', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  updateClientCredentials: (clientId: string, payload: any) =>
    request<{ status: string; message?: string }>(`/api/clients/${clientId}/credentials`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),

  getClientSettings: (clientId: string) =>
    request<ClientSettings>(`/api/clients/${clientId}/settings`),

  updateClientRiskLimits: (clientId: string, payload: any) =>
    request<{ status: string; message?: string }>(`/api/clients/${clientId}/risk-limits`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),

  rotateWebhookSecret: (clientId: string) =>
    request<{ status: string; webhook_secret: string; webhook_url: string }>(`/api/clients/${clientId}/webhook-secret/rotate`, {
      method: 'POST',
    }),

  squareOffPosition: (clientId: string, symbol: string, quantity?: number, side?: string, productType?: string) =>
    request<{ status: string; result?: any; message?: string }>(`/api/clients/${clientId}/positions/square-off`, {
      method: 'POST',
      body: JSON.stringify({ symbol, quantity, side, product_type: productType }),
    }),

  deleteClient: (clientId: string) =>
    request<{ status: string }>(`/api/clients/${clientId}`, { method: 'DELETE' }),

  // Orders
  getGlobalOrders: (status?: string, search?: string) => {
    const params = new URLSearchParams();
    if (status) params.set('status', status);
    if (search) params.set('search', search);
    return request<{ orders: OrderItem[] }>(`/api/orders?${params.toString()}`);
  },

  cancelOrder: (clientId: string, appOrderId: string) =>
    request<{ status: string; result?: any }>(`/api/clients/${clientId}/orders/${appOrderId}/cancel`, { method: 'POST' }),

  bulkCancelOrders: (tenantId?: string) =>
    request<{ status: string; cancelled_clients?: number; results?: any }>(`/api/orders/bulk-cancel`, {
      method: 'POST',
      body: JSON.stringify({ tenant_id: tenantId }),
    }),


  // Strategies & Charting
  getStrategies: (clientId: string) =>
    request<{ strategies: StrategyItem[] }>(`/api/clients/${clientId}/strategies`),

  saveStrategy: (clientId: string, payload: any) =>
    request<{ status: string; strategy_id: string }>(`/api/clients/${clientId}/strategies`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  toggleStrategy: (clientId: string, stratId: string) =>
    request<{ status: string; is_enabled: boolean }>(`/api/clients/${clientId}/strategies/${stratId}/toggle`, {
      method: 'POST',
    }),

  deleteStrategy: (clientId: string, stratId: string) =>
    request<{ status: string }>(`/api/clients/${clientId}/strategies/${stratId}`, {
      method: 'DELETE',
    }),

  evaluateStrategyNow: (clientId: string, symbol: string, timeframe?: string, stratId?: string) =>
    request<{ status: string; result: any }>(`/api/clients/${clientId}/strategies/evaluate-now`, {
      method: 'POST',
      body: JSON.stringify({ symbol, timeframe, strategy_id: stratId }),
    }),

  syncStrategyTrend: (clientId: string, symbol: string, timeframe: string, targetTrend: string, stratId?: string) =>
    request<{ status: string; message: string }>(`/api/clients/${clientId}/strategies/sync-trend`, {
      method: 'POST',
      body: JSON.stringify({ symbol, timeframe, target_trend: targetTrend, strategy_id: stratId }),
    }),

  resetStrategyFlat: (clientId: string, symbol: string, timeframe: string, stratId?: string) =>
    request<{ status: string; message: string }>(`/api/clients/${clientId}/strategies/reset-flat`, {
      method: 'POST',
      body: JSON.stringify({ symbol, timeframe, strategy_id: stratId }),
    }),

  getCandles: (clientId: string, symbol: string, timeframe: string = '5m', limit: number = 300) =>
    request<{
      candles: CandleData[];
      supertrend_line: Array<{ time: number; value: number; color: string }>;
      upper_band?: Array<{ time: number; value: number }>;
      lower_band?: Array<{ time: number; value: number }>;
      markers: ChartMarker[];
      symbol: string;
      timeframe: string;
    }>(`/api/clients/${clientId}/candles?symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}&limit=${limit}`),

  // Audit Logs
  getAuditLogs: (limit: number = 100) =>
    request<{ logs: Array<{ id: string; formatted_time: string; actor: string; action: string; target_tenant_id?: string; details_json: string }> }>(`/api/audit-logs?limit=${limit}`),
};
