import React, { useState, useEffect, useMemo } from 'react';
import { OrderItem } from '../../types/telemetry';
import { api } from '../../services/api';
import { Search, RotateCw, Download, XCircle, ArrowUpRight, ArrowDownRight } from 'lucide-react';
import { toast } from 'sonner';

export const GlobalOrdersView: React.FC = () => {
  const [orders, setOrders] = useState<OrderItem[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState<string>('ALL');
  const [sideFilter, setSideFilter] = useState<string>('ALL');

  const fetchOrders = async () => {
    setIsLoading(true);
    try {
      const res = await api.getGlobalOrders();
      setOrders(res.orders || []);
    } catch (err: any) {
      toast.error(`Failed to load orders: ${err.message}`);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    fetchOrders();
    const interval = setInterval(fetchOrders, 5000);
    return () => clearInterval(interval);
  }, []);

  const [isBulkCancelling, setIsBulkCancelling] = useState(false);

  const handleCancelOrder = async (clientId: string, appOrderId: string) => {
    toast.info(`Cancelling order ${appOrderId}...`);
    try {
      await api.cancelOrder(clientId, appOrderId);
      toast.success(`Order ${appOrderId} cancelled successfully`);
      fetchOrders();
    } catch (err: any) {
      toast.error(`Cancel failed: ${err.message}`);
    }
  };

  const openOrdersCount = useMemo(() => {
    return orders.filter((o) => ['OPEN', 'PENDING', 'TRIGGER_PENDING'].includes(o.status.toUpperCase())).length;
  }, [orders]);

  const handleBulkCancel = async () => {
    if (openOrdersCount === 0) {
      toast.info('No open orders to cancel');
      return;
    }
    setIsBulkCancelling(true);
    toast.info(`Cancelling all ${openOrdersCount} open orders across all accounts...`);
    try {
      await api.bulkCancelOrders();
      toast.success('Bulk cancellation request dispatched successfully');
      fetchOrders();
    } catch (err: any) {
      toast.error(`Bulk cancel failed: ${err.message}`);
    } finally {
      setIsBulkCancelling(false);
    }
  };

  const handleExportCSV = () => {
    if (filteredOrders.length === 0) {
      toast.error('No orders available to export');
      return;
    }

    const headers = ['Client Name', 'Client ID', 'App Order ID', 'Time (Placed)', 'Symbol', 'Side', 'Quantity', 'Price', 'Status', 'Product', 'Exchange Segment', 'Reject / Status Message'];
    const rows = filteredOrders.map((o) => [
      `"${(o.client_name || o.client_id || '').replace(/"/g, '""')}"`,
      `"${(o.client_id || '').replace(/"/g, '""')}"`,
      `"${(o.app_order_id || '').replace(/"/g, '""')}"`,
      `"${(o.placed_at || '').replace(/"/g, '""')}"`,
      `"${(o.symbol || '').replace(/"/g, '""')}"`,
      `"${(o.side || '').replace(/"/g, '""')}"`,
      o.quantity || 0,
      o.price || 0,
      `"${(o.status || '').replace(/"/g, '""')}"`,
      `"${(o.product_type || '').replace(/"/g, '""')}"`,
      `"${(o.exchange_segment || '').replace(/"/g, '""')}"`,
      `"${(o.status_message || '').replace(/"/g, '""')}"`
    ]);

    const csvContent = [headers.join(','), ...rows.map((r) => r.join(','))].join('\n');
    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    const nowStr = new Date().toISOString().replace(/\D/g, '').slice(0, 14);
    link.href = url;
    link.setAttribute('download', `orders_${nowStr}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
    toast.success(`Exported ${filteredOrders.length} orders to orders_${nowStr}.csv`);
  };

  const filteredOrders = useMemo(() => {
    return orders.filter((o) => {
      const q = search.toLowerCase().trim();
      const matchSearch =
        !q ||
        o.app_order_id.toLowerCase().includes(q) ||
        o.symbol.toLowerCase().includes(q) ||
        (o.client_id && o.client_id.toLowerCase().includes(q)) ||
        (o.client_name && o.client_name.toLowerCase().includes(q));

      if (!matchSearch) return false;

      if (statusFilter !== 'ALL') {
        if (statusFilter === 'OPEN' && !['OPEN', 'PENDING', 'TRIGGER_PENDING'].includes(o.status.toUpperCase())) return false;
        if (statusFilter === 'COMPLETE' && o.status.toUpperCase() !== 'COMPLETE') return false;
        if (statusFilter === 'REJECTED' && o.status.toUpperCase() !== 'REJECTED') return false;
        if (statusFilter === 'CANCELLED' && o.status.toUpperCase() !== 'CANCELLED') return false;
      }

      if (sideFilter !== 'ALL' && o.side.toUpperCase() !== sideFilter.toUpperCase()) {
        return false;
      }

      return true;
    });
  }, [orders, search, statusFilter, sideFilter]);

  return (
    <div className="flex-1 flex flex-col overflow-hidden p-4 md:p-6 space-y-4">
      {/* Title and Controls */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h2 className="text-base font-bold text-slate-100 flex items-center space-x-2">
            <span>Global Order Book</span>
            <span className="text-xs font-mono px-2 py-0.5 rounded bg-slate-800 text-slate-400">
              {filteredOrders.length} Orders
            </span>
          </h2>
          <p className="text-xs text-slate-400">Multi-tenant consolidated order execution log</p>
        </div>

        <div className="flex items-center space-x-2">
          {/* Export CSV */}
          <button
            type="button"
            onClick={handleExportCSV}
            className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 rounded-xl border border-bordercolor font-semibold text-xs flex items-center space-x-1.5 transition"
            title="Export Orders as CSV"
          >
            <Download className="w-3.5 h-3.5 text-brand-400" />
            <span>Export CSV</span>
          </button>

          {/* Cancel All Open Orders */}
          {openOrdersCount > 0 && (
            <button
              type="button"
              onClick={handleBulkCancel}
              disabled={isBulkCancelling}
              className="px-3 py-1.5 bg-rose-600/20 hover:bg-rose-600 text-rose-300 hover:text-white border border-rose-500/30 rounded-xl font-semibold text-xs flex items-center space-x-1.5 transition"
              title="Cancel all open orders across accounts"
            >
              <XCircle className="w-3.5 h-3.5" />
              <span>Cancel All Open ({openOrdersCount})</span>
            </button>
          )}

          <button
            type="button"
            onClick={fetchOrders}
            disabled={isLoading}
            className="p-2 bg-slate-800 hover:bg-slate-750 text-slate-200 rounded-xl border border-bordercolor transition"
            title="Refresh Orders"
          >
            <RotateCw className={`w-4 h-4 ${isLoading ? 'animate-spin text-brand-400' : ''}`} />
          </button>
        </div>
      </div>

      {/* Filter Bar */}
      <div className="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-3 bg-cardbg/80 border border-bordercolor p-3 rounded-2xl">
        <div className="relative flex-1 max-w-md">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search by symbol, order ID, or client..."
            className="w-full pl-9 pr-4 py-2 rounded-xl bg-obsidian border border-bordercolor text-xs text-slate-100 placeholder-slate-500 focus:outline-none focus:border-brand-500 transition"
          />
        </div>

        {/* Status Filters */}
        <div className="flex items-center space-x-1 overflow-x-auto no-scrollbar py-0.5 text-xs">
          {(['ALL', 'COMPLETE', 'OPEN', 'REJECTED', 'CANCELLED'] as const).map((st) => (
            <button
              key={st}
              type="button"
              onClick={() => setStatusFilter(st)}
              className={`px-3 py-1.5 rounded-xl font-medium text-[11px] transition ${
                statusFilter === st
                  ? 'bg-brand-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800'
              }`}
            >
              {st}
            </button>
          ))}
        </div>

        {/* Side Filters */}
        <div className="flex items-center space-x-1 border-l border-bordercolor pl-2 shrink-0 text-xs">
          {(['ALL', 'BUY', 'SELL'] as const).map((sd) => (
            <button
              key={sd}
              type="button"
              onClick={() => setSideFilter(sd)}
              className={`px-2.5 py-1 rounded-lg font-mono text-[11px] transition ${
                sideFilter === sd
                  ? sd === 'BUY'
                    ? 'bg-emerald-600 text-white'
                    : sd === 'SELL'
                    ? 'bg-rose-600 text-white'
                    : 'bg-slate-800 text-white'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              {sd}
            </button>
          ))}
        </div>
      </div>

      {/* Orders Table */}
      <div className="flex-1 bg-cardbg border border-bordercolor rounded-2xl overflow-hidden shadow-sm flex flex-col">
        <div className="flex-1 overflow-y-auto">
          <table className="w-full text-left text-xs font-mono">
            <thead className="bg-slate-950/80 text-slate-400 text-[11px] uppercase tracking-wider border-b border-bordercolor sticky top-0 z-10 select-none">
              <tr>
                <th className="p-3">Client</th>
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
              {filteredOrders.length === 0 ? (
                <tr>
                  <td colSpan={10} className="p-12 text-center text-slate-500 italic">
                    No orders match the selected filters.
                  </td>
                </tr>
              ) : (
                filteredOrders.map((ord, idx) => {
                  const isOpen = ['OPEN', 'PENDING', 'TRIGGER_PENDING'].includes(ord.status.toUpperCase());
                  return (
                    <tr key={idx} className="hover:bg-slate-800/40 transition">
                      <td className="p-3 font-semibold">
                        <div className="text-slate-100">{ord.client_name || ord.client_id || 'Unknown'}</div>
                        <div className="text-[10px] text-slate-500">{ord.client_id}</div>
                      </td>
                      <td className="p-3 text-slate-300 font-mono">{ord.app_order_id}</td>
                      <td className="p-3 text-slate-400">{ord.placed_at || '--:--:--'}</td>
                      <td className="p-3 font-bold text-slate-100">{ord.symbol}</td>
                      <td className="p-3">
                        <span
                          className={`inline-flex items-center space-x-1 px-2 py-0.5 rounded text-[10px] font-bold ${
                            ord.side === 'BUY' ? 'bg-emerald-500/20 text-emerald-400' : 'bg-rose-500/20 text-rose-400'
                          }`}
                        >
                          {ord.side === 'BUY' ? (
                            <ArrowUpRight className="w-3 h-3" />
                          ) : (
                            <ArrowDownRight className="w-3 h-3" />
                          )}
                          <span>{ord.side}</span>
                        </span>
                      </td>
                      <td className="p-3 text-right text-slate-100">{ord.quantity}</td>
                      <td className="p-3 text-right text-slate-300">{ord.price || 'MKT'}</td>
                      <td className="p-3">
                        <span
                          className={`px-2 py-0.5 rounded text-[10px] font-semibold ${
                            ord.status === 'COMPLETE'
                              ? 'bg-emerald-500/10 text-emerald-400'
                              : ord.status === 'REJECTED'
                              ? 'bg-rose-500/10 text-rose-400'
                              : isOpen
                              ? 'bg-amber-500/10 text-amber-400'
                              : 'bg-slate-800 text-slate-400'
                          }`}
                        >
                          {ord.status}
                        </span>
                      </td>
                      <td className="p-3 text-slate-400 truncate max-w-xs">{ord.status_message || '-'}</td>
                      <td className="p-3 text-right">
                        {isOpen && ord.client_id && (
                          <button
                            type="button"
                            onClick={() => handleCancelOrder(ord.client_id!, ord.app_order_id)}
                            className="p-1 text-rose-400 hover:text-rose-300 hover:bg-rose-500/10 rounded-lg transition"
                            title="Cancel Order"
                          >
                            <XCircle className="w-4 h-4 inline" />
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
    </div>
  );
};
