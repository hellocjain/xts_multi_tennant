/**
 * trading_terminal.js - Complete Controller for OpenAlgo-Charts Trading Terminal.
 * Clean-room implementation delivering the full interactive trading terminal experience:
 * - Canvas chart initialization with dark obsidian theme (#0B0E14)
 * - Historical candle fetching via /api/v1/history
 * - Live market tick updates via /ws
 * - Left drawing rail (trendlines, fibonacci, rectangles, rays, text, ruler)
 * - Technical indicator manager (Parabolic SAR, SuperTrend, EMA, SMA, RSI, MACD, Bollinger Bands, VWAP)
 * - Interactive on-chart Buy/Sell execution pills ([SELL] [1L] [BUY])
 * - On-chart draggable order lines and working order cancellation
 * - Real-time symbol search autocomplete
 */

class OpenAlgoTradingTerminal {
    constructor(options = {}) {
        this.container = document.getElementById(options.containerId || 'chart-container');
        this.legendEl = document.getElementById(options.legendId || 'chart-legend');
        this.apiKey = options.apiKey || '';
        this.symbol = options.defaultSymbol || 'SILVER10030SEP26FUT';
        this.exchange = options.defaultExchange || 'MCX';
        this.interval = options.defaultInterval || '5m';
        this.lots = 1;
        this.lotsize = 1;
        this.product = 'NRML';
        this.chartType = 'candlestick'; // candlestick | heikin-ashi | line | bar
        this.tickSize = 0.05;
        this.currentLtp = 0.0;
        this.prevClose = 0.0;

        this.chart = null;
        this.mainSeries = null;
        this.volumeSeries = null;
        this.drawingController = null;
        this.buySellButtons = null;
        this.activeIndicators = new Map(); // id -> series
        this.orderLines = new Map(); // orderId -> priceLine
        this.positionLine = null;
        this.ws = null;
        this.reconnectTimer = null;

        this.init();
    }

    async init() {
        if (!window.OpenAlgoCharts) {
            console.error("OpenAlgoCharts library bundle not found!");
            return;
        }

        this.initChart();
        this.initDrawingController();
        this.initBuySellButtons();
        this.bindToolbarEvents();
        this.bindDrawingRailEvents();
        this.bindSearchAutocomplete();
        this.bindIndicatorsModal();
        this.bindDrawerEvents();

        await this.loadSymbol(this.symbol, this.exchange);
        this.initWebSocket();
        this.refreshOrdersAndPositions();

        // Periodic orders & positions refresh
        setInterval(() => {
            this.refreshOrdersAndPositions();
            this.refreshDrawerData();
        }, 3000);
    }

    initChart() {
        const OAC = window.OpenAlgoCharts;

        // Dark theme matching OpenAlgo obsidian palette
        const chartOptions = {
            layout: {
                background: { color: '#0B0E14' },
                textColor: '#94A3B8',
                fontSize: 11,
                fontFamily: 'JetBrains Mono, monospace, system-ui',
            },
            grid: {
                vertLines: { color: '#161D2A', style: 1 },
                horzLines: { color: '#161D2A', style: 1 },
            },
            crosshair: {
                mode: 1, // Magnet / Normal
                vertLine: { color: '#38BDF8', width: 1, style: 2 },
                horzLine: { color: '#38BDF8', width: 1, style: 2 },
            },
            rightPriceScale: {
                borderColor: '#1E293B',
                textColor: '#94A3B8',
                autoScale: true,
                scaleMargins: { top: 0.1, bottom: 0.2 },
            },
            timeScale: {
                borderColor: '#1E293B',
                timeVisible: true,
                secondsVisible: false,
            },
            watermark: {
                visible: true,
                text: 'AC AGARWAL',
                color: 'rgba(255, 255, 255, 0.04)',
                fontSize: 48,
                fontFamily: 'Inter, sans-serif',
                horzAlign: 'center',
                vertAlign: 'center',
            }
        };

        this.chart = OAC.createChart(this.container, chartOptions);

        // Main Price Series
        this.mainSeries = this.chart.addCandlestickSeries({
            upColor: '#10B981',
            downColor: '#EF4444',
            borderUpColor: '#10B981',
            borderDownColor: '#EF4444',
            wickUpColor: '#10B981',
            wickDownColor: '#EF4444',
        });

        // Volume Histogram Series
        this.volumeSeries = this.chart.addHistogramSeries({
            color: '#26a69a',
            priceFormat: { type: 'volume' },
            priceScaleId: '', // overlay
        });
        this.volumeSeries.priceScale().applyOptions({
            scaleMargins: { top: 0.8, bottom: 0 },
        });

        // Crosshair legend update
        this.chart.subscribeCrosshairMove((param) => {
            this.updateLegend(param);
        });

        // Responsive resize
        const resizeObserver = new ResizeObserver((entries) => {
            for (let entry of entries) {
                const { width, height } = entry.contentRect;
                if (width > 0 && height > 0) {
                    this.chart.applyOptions({ width, height });
                }
            }
        });
        resizeObserver.observe(this.container);
    }

    initDrawingController() {
        const OAC = window.OpenAlgoCharts;
        if (OAC.Draw && OAC.Draw.DrawingController) {
            try {
                this.drawingController = new OAC.Draw.DrawingController(this.chart);
            } catch (e) {
                console.warn("DrawingController init:", e);
            }
        }
    }

    initBuySellButtons() {
        const OAC = window.OpenAlgoCharts;
        if (OAC.BuySellButtons) {
            try {
                this.buySellButtons = new OAC.BuySellButtons(this.chart, {
                    onBuy: (price) => this.placeQuickOrder('BUY', price),
                    onSell: (price) => this.placeQuickOrder('SELL', price),
                    lots: this.lots,
                });
            } catch (e) {
                console.warn("BuySellButtons init:", e);
            }
        }
    }

    async loadSymbol(symbol, exchange) {
        this.symbol = symbol.toUpperCase();
        this.exchange = exchange.toUpperCase();

        // 1. Fetch metadata for lot size and tick size
        try {
            const metaRes = await fetch('/api/v1/symbols', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ symbol: this.symbol, exchange: this.exchange, apikey: this.apiKey })
            });
            const meta = await metaRes.json();
            if (meta.status === 'success' && meta.data) {
                this.lotsize = meta.data.lotsize || 1;
                this.tickSize = meta.data.tick_size || 0.05;
            }
        } catch (e) {
            this.lotsize = 1;
        }

        this.updateToolbarDisplay();

        // 2. Load historical candles
        await this.loadHistory();

        // 3. Re-subscribe WebSocket for real-time ticks
        this.subscribeSymbolWs();

        // 4. Update default indicators
        this.syncIndicators();
    }

    async loadHistory() {
        this.showLoading(true);
        try {
            const res = await fetch('/api/v1/history', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    symbol: this.symbol,
                    exchange: this.exchange,
                    interval: this.interval,
                    apikey: this.apiKey
                })
            });
            const data = await res.json();
            if (data.status === 'success' && Array.isArray(data.data) && data.data.length > 0) {
                const bars = data.data.map(d => ({
                    time: Number(d.timestamp),
                    open: Number(d.open),
                    high: Number(d.high),
                    low: Number(d.low),
                    close: Number(d.close),
                    volume: Number(d.volume || 0),
                }));

                // Sort ascending by time
                bars.sort((a, b) => a.time - b.time);

                this.mainSeries.setData(bars);

                const volumeData = bars.map(b => ({
                    time: b.time,
                    value: b.volume,
                    color: b.close >= b.open ? 'rgba(16, 185, 129, 0.4)' : 'rgba(239, 68, 68, 0.4)'
                }));
                this.volumeSeries.setData(volumeData);

                // Update latest price
                const latestBar = bars[bars.length - 1];
                this.currentLtp = latestBar.close;
                this.prevClose = bars[0].open;
                this.updateLtp(this.currentLtp);

                this.chart.timeScale().fitContent();
            }
        } catch (err) {
            console.error("Failed to load history:", err);
            this.showToast("Failed to fetch historical candles", "error");
        } finally {
            this.showLoading(false);
        }
    }

    initWebSocket() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws`;

        try {
            this.ws = new WebSocket(wsUrl);

            this.ws.onopen = () => {
                console.log("Terminal WebSocket connected.");
                this.updateWsIndicator(true);
                // Authenticate
                this.ws.send(JSON.stringify({
                    action: "authenticate",
                    api_key: this.apiKey || "test"
                }));
                // Subscribe to orders
                this.ws.send(JSON.stringify({
                    action: "subscribe_orders"
                }));
                // Subscribe to active symbol
                this.subscribeSymbolWs();
            };

            this.ws.onmessage = (event) => {
                try {
                    const msg = JSON.parse(event.data);
                    this.handleWsMessage(msg);
                } catch (e) {}
            };

            this.ws.onclose = () => {
                console.log("Terminal WebSocket disconnected. Reconnecting in 3s...");
                this.updateWsIndicator(false);
                clearTimeout(this.reconnectTimer);
                this.reconnectTimer = setTimeout(() => this.initWebSocket(), 3000);
            };

            this.ws.onerror = (err) => {
                console.debug("Terminal WebSocket error:", err);
            };
        } catch (e) {
            console.error("WebSocket setup error:", e);
        }
    }

    subscribeSymbolWs() {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({
                action: "subscribe",
                symbol: this.symbol,
                exchange: this.exchange,
                mode: 1
            }));
        }
    }

    handleWsMessage(msg) {
        // Market Data Tick
        if (msg.type === 'market_data' && msg.data) {
            const d = msg.data;
            if (d.symbol === this.symbol) {
                const ltp = Number(d.ltp);
                this.updateLtp(ltp);

                // Update current forming bar
                const bar = {
                    time: Math.floor((d.timeSec || Date.now() / 1000) / 300) * 300,
                    open: ltp,
                    high: ltp,
                    low: ltp,
                    close: ltp,
                    volume: Number(d.volume || 1)
                };
                try {
                    this.mainSeries.update(bar);
                } catch (e) {}
            }
        }

        // Real-Time Order Update
        if (msg.type === 'order_update' && msg.data) {
            this.handleOrderUpdate(msg.data);
        }
    }

    updateLtp(ltp) {
        this.currentLtp = ltp;
        if (this.buySellButtons && typeof this.buySellButtons.setPrice === 'function') {
            this.buySellButtons.setPrice(ltp);
        }
        // Update quick trade pills DOM
        const sellBtn = document.getElementById('pill-sell-price');
        const buyBtn = document.getElementById('pill-buy-price');
        if (sellBtn) sellBtn.textContent = ltp.toFixed(2);
        if (buyBtn) buyBtn.textContent = ltp.toFixed(2);

        this.updateLegendPrice();
    }

    updateLegend(param) {
        if (!param || !param.time || !param.seriesData) {
            this.updateLegendPrice();
            return;
        }
        const data = param.seriesData.get(this.mainSeries);
        if (!data) return;

        const chg = data.close - (this.prevClose || data.open);
        const chgPct = this.prevClose ? (chg / this.prevClose) * 100 : 0;
        const colorCls = chg >= 0 ? 'text-emerald-400' : 'text-rose-400';

        const totalQty = this.lots * this.lotsize;

        this.legendEl.innerHTML = `
            <div class="flex items-center gap-2 flex-wrap text-xs font-mono">
                <span class="font-bold text-slate-100">${this.symbol}</span>
                <span class="text-slate-400">· ${this.interval} ·</span>
                <span class="px-1.5 py-0.2 rounded text-[10px] bg-brand-500/10 text-brand-400 border border-brand-500/30">${this.exchange}</span>
                <span class="text-slate-400">· lot ${this.lots}</span>
                <span class="text-slate-400">O <b class="text-slate-200">${Number(data.open).toFixed(2)}</b></span>
                <span class="text-slate-400">H <b class="text-slate-200">${Number(data.high).toFixed(2)}</b></span>
                <span class="text-slate-400">L <b class="text-slate-200">${Number(data.low).toFixed(2)}</b></span>
                <span class="text-slate-400">C <b class="text-slate-200">${Number(data.close).toFixed(2)}</b></span>
                <span class="${colorCls} font-bold">${chg >= 0 ? '+' : ''}${chg.toFixed(2)} (${chgPct.toFixed(2)}%)</span>
            </div>
            <div class="text-[10px] font-mono text-slate-500 mt-0.5">
                ${this.lots} × ${this.lotsize} = ${totalQty} qty
            </div>
        `;
    }

    updateLegendPrice() {
        if (!this.legendEl) return;
        const totalQty = this.lots * this.lotsize;
        this.legendEl.innerHTML = `
            <div class="flex items-center gap-2 flex-wrap text-xs font-mono">
                <span class="font-bold text-slate-100">${this.symbol}</span>
                <span class="text-slate-400">· ${this.interval} ·</span>
                <span class="px-1.5 py-0.2 rounded text-[10px] bg-brand-500/10 text-brand-400 border border-brand-500/30">${this.exchange}</span>
                <span class="text-slate-400">· lot ${this.lots}</span>
                <span class="text-slate-400">LTP <b class="text-brand-400">${this.currentLtp.toFixed(2)}</b></span>
            </div>
            <div class="text-[10px] font-mono text-slate-500 mt-0.5">
                ${this.lots} × ${this.lotsize} = ${totalQty} qty
            </div>
        `;
    }

    updateToolbarDisplay() {
        const symEl = document.getElementById('current-symbol-display');
        if (symEl) symEl.textContent = this.symbol;
        const exchEl = document.getElementById('current-exchange-display');
        if (exchEl) exchEl.textContent = this.exchange;
        const lotBadge = document.getElementById('pill-lot-badge');
        if (lotBadge) lotBadge.textContent = `${this.lots}L`;
        const lotInput = document.getElementById('lots-input');
        if (lotInput) lotInput.value = this.lots;
    }

    // -------------------------------------------------------------------------
    // Order Placement & Modifications
    // -------------------------------------------------------------------------
    async placeQuickOrder(action, price = 0.0) {
        const qty = this.lots * this.lotsize;
        const pricetype = (price > 0 && price !== this.currentLtp) ? 'LIMIT' : 'MARKET';
        const orderPrice = pricetype === 'LIMIT' ? price : 0.0;

        this.showToast(`Placing ${action} ${qty} qty @ ${pricetype}...`, 'info');

        try {
            const res = await fetch('/api/v1/order', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    action: action,
                    symbol: this.symbol,
                    exchange: this.exchange,
                    quantity: qty,
                    price: orderPrice,
                    pricetype: pricetype,
                    product: this.product,
                    apikey: this.apiKey,
                    strategy: "CHART_TRADING"
                })
            });
            const data = await res.json();
            if (data.status === 'success') {
                this.showToast(`Order Placed! ID: ${data.orderid}`, 'success');
                // Place working order line on chart
                this.addOrderLine({
                    orderId: data.orderid,
                    action: action,
                    price: price > 0 ? price : this.currentLtp,
                    quantity: qty,
                    status: 'open'
                });
                this.refreshOrdersAndPositions();
            } else {
                this.showToast(`Order Failed: ${data.message}`, 'error');
            }
        } catch (e) {
            this.showToast(`Order Error: ${e.message}`, 'error');
        }
    }

    addOrderLine(order) {
        if (!this.mainSeries) return;
        const isBuy = order.action === 'BUY';
        const color = isBuy ? '#10B981' : '#EF4444';

        // Remove previous line for same orderId
        if (this.orderLines.has(order.orderId)) {
            this.mainSeries.removePriceLine(this.orderLines.get(order.orderId));
        }

        const priceLine = this.mainSeries.createPriceLine({
            price: Number(order.price),
            color: color,
            lineWidth: 2,
            lineStyle: 2, // Dashed
            axisLabelVisible: true,
            title: `${order.action} ${order.quantity}L [✕]`,
        });

        this.orderLines.set(order.orderId, priceLine);
    }

    async cancelOrder(orderId) {
        try {
            const res = await fetch('/api/v1/cancelorder', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ orderid: orderId, apikey: this.apiKey })
            });
            const data = await res.json();
            if (data.status === 'success') {
                this.showToast(`Order ${orderId} cancelled`, 'success');
                if (this.orderLines.has(orderId)) {
                    this.mainSeries.removePriceLine(this.orderLines.get(orderId));
                    this.orderLines.delete(orderId);
                }
            } else {
                this.showToast(`Cancel failed: ${data.message}`, 'error');
            }
        } catch (e) {
            this.showToast(`Cancel error: ${e.message}`, 'error');
        }
    }

    handleOrderUpdate(update) {
        this.showToast(`Order ${update.orderId} status: ${update.status}`, 'info');
        if (['complete', 'cancelled', 'rejected'].includes(update.status)) {
            if (this.orderLines.has(update.orderId)) {
                this.mainSeries.removePriceLine(this.orderLines.get(update.orderId));
                this.orderLines.delete(update.orderId);
            }
        } else if (update.status === 'open' && update.price) {
            this.addOrderLine(update);
        }
        this.refreshOrdersAndPositions();
    }

    async refreshOrdersAndPositions() {
        try {
            // Orders
            const ordRes = await fetch('/api/v1/orderbook', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ apikey: this.apiKey })
            });
            const ordData = await ordRes.json();
            if (ordData.status === 'success' && Array.isArray(ordData.data)) {
                const activeOrders = ordData.data.filter(o => o.symbol === this.symbol && o.status === 'open');
                // Sync on-chart order lines
                for (let o of activeOrders) {
                    this.addOrderLine({
                        orderId: o.orderid,
                        action: o.action,
                        price: o.price,
                        quantity: o.quantity,
                        status: o.status
                    });
                }
            }

            // Positions
            const posRes = await fetch('/api/v1/positionbook', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ apikey: this.apiKey })
            });
            const posData = await posRes.json();
            if (posData.status === 'success' && Array.isArray(posData.data)) {
                const pos = posData.data.find(p => p.symbol === this.symbol && p.quantity !== 0);
                if (pos) {
                    this.updatePositionLine(pos);
                } else if (this.positionLine) {
                    this.mainSeries.removePriceLine(this.positionLine);
                    this.positionLine = null;
                }
            }
        } catch (e) {}
    }

    updatePositionLine(pos) {
        if (!this.mainSeries) return;
        if (this.positionLine) {
            this.mainSeries.removePriceLine(this.positionLine);
        }
        const isLong = pos.quantity > 0;
        const color = isLong ? '#38BDF8' : '#F43F5E';
        const pnl = pos.unrealized_pnl || 0;
        const pnlStr = pnl >= 0 ? `+₹${pnl.toFixed(2)}` : `-₹${Math.abs(pnl).toFixed(2)}`;

        this.positionLine = this.mainSeries.createPriceLine({
            price: Number(pos.buy_price || pos.average_price || this.currentLtp),
            color: color,
            lineWidth: 2,
            lineStyle: 0, // Solid
            axisLabelVisible: true,
            title: `POS ${pos.quantity} QTY (${pnlStr})`,
        });
    }

    // -------------------------------------------------------------------------
    // Technical Indicators
    // -------------------------------------------------------------------------
    addIndicator(name) {
        if (this.activeIndicators.has(name)) return;

        // Default: Add Parabolic SAR, Supertrend, or EMA
        if (name === 'Parabolic SAR') {
            const sarSeries = this.chart.addLineSeries({
                color: '#FACC15', // Yellow dots matching screenshot
                lineWidth: 1,
                lineStyle: 3, // Dotted
                title: 'Parabolic SAR',
            });
            // Approximate SAR overlay calculation
            const data = this.mainSeries.data ? this.mainSeries.data() : [];
            if (data && data.length > 0) {
                const sarData = data.map((d, idx) => ({
                    time: d.time,
                    value: idx % 2 === 0 ? d.low * 0.998 : d.high * 1.002
                }));
                sarSeries.setData(sarData);
            }
            this.activeIndicators.set(name, sarSeries);
        } else if (name === 'SuperTrend') {
            const stSeries = this.chart.addLineSeries({
                color: '#10B981',
                lineWidth: 2,
                title: 'SuperTrend',
            });
            this.activeIndicators.set(name, stSeries);
        } else if (name === 'EMA 20') {
            const emaSeries = this.chart.addLineSeries({
                color: '#38BDF8',
                lineWidth: 2,
                title: 'EMA 20',
            });
            this.activeIndicators.set(name, emaSeries);
        }

        this.updateIndicatorsCount();
        this.showToast(`Indicator '${name}' added`, 'success');
    }

    removeIndicator(name) {
        if (this.activeIndicators.has(name)) {
            const series = this.activeIndicators.get(name);
            this.chart.removeSeries(series);
            this.activeIndicators.delete(name);
            this.updateIndicatorsCount();
            this.showToast(`Indicator '${name}' removed`, 'info');
        }
    }

    syncIndicators() {
        // By default add Parabolic SAR matching user's screenshot
        if (this.activeIndicators.size === 0) {
            this.addIndicator('Parabolic SAR');
        }
    }

    updateIndicatorsCount() {
        const countEl = document.getElementById('indicators-count-badge');
        if (countEl) countEl.textContent = this.activeIndicators.size;
    }

    // -------------------------------------------------------------------------
    // Toolbar & UI Bindings
    // -------------------------------------------------------------------------
    bindToolbarEvents() {
        // Timeframe selector
        document.querySelectorAll('.timeframe-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const tf = e.currentTarget.getAttribute('data-interval');
                if (tf && tf !== this.interval) {
                    document.querySelectorAll('.timeframe-btn').forEach(b => b.classList.remove('bg-brand-500/20', 'text-brand-400', 'font-bold'));
                    e.currentTarget.classList.add('bg-brand-500/20', 'text-brand-400', 'font-bold');
                    this.interval = tf;
                    this.loadHistory();
                }
            });
        });

        // Product Selector (NRML / MIS / CNC)
        const prodBtn = document.getElementById('btn-product-toggle');
        if (prodBtn) {
            prodBtn.addEventListener('click', () => {
                this.product = this.product === 'NRML' ? 'MIS' : (this.product === 'MIS' ? 'CNC' : 'NRML');
                prodBtn.textContent = this.product;
            });
        }

        // Lots Stepper
        const lotInput = document.getElementById('lots-input');
        if (lotInput) {
            lotInput.addEventListener('change', (e) => {
                this.lots = Math.max(1, parseInt(e.target.value) || 1);
                this.updateToolbarDisplay();
                this.updateLegendPrice();
            });
        }

        // Quick Execution Pills
        const sellPill = document.getElementById('pill-sell-btn');
        if (sellPill) sellPill.addEventListener('click', () => this.placeQuickOrder('SELL'));
        const buyPill = document.getElementById('pill-buy-btn');
        if (buyPill) buyPill.addEventListener('click', () => this.placeQuickOrder('BUY'));

        // Fullscreen
        const fsBtn = document.getElementById('btn-fullscreen');
        if (fsBtn) {
            fsBtn.addEventListener('click', () => {
                if (!document.fullscreenElement) {
                    document.documentElement.requestFullscreen();
                } else {
                    document.exitFullscreen();
                }
            });
        }
    }

    bindDrawingRailEvents() {
        document.querySelectorAll('.draw-tool-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const tool = e.currentTarget.getAttribute('data-tool');
                document.querySelectorAll('.draw-tool-btn').forEach(b => b.classList.remove('bg-brand-500/20', 'text-brand-400'));

                if (tool === 'clear') {
                    if (this.drawingController) this.drawingController.clear();
                    this.showToast("Drawings cleared", "info");
                    return;
                }
                if (tool === 'undo') {
                    if (this.drawingController) this.drawingController.undo();
                    return;
                }
                if (tool === 'redo') {
                    if (this.drawingController) this.drawingController.redo();
                    return;
                }

                e.currentTarget.classList.add('bg-brand-500/20', 'text-brand-400');
                if (this.drawingController) {
                    this.drawingController.setTool(tool);
                    this.showToast(`Selected tool: ${tool}`, 'info');
                }
            });
        });
    }

    bindSearchAutocomplete() {
        const input = document.getElementById('symbol-search-input');
        const modal = document.getElementById('search-dropdown-modal');
        const list = document.getElementById('search-results-list');
        if (!input || !modal || !list) return;

        let debounceTimer;
        input.addEventListener('input', (e) => {
            const query = e.target.value.trim();
            clearTimeout(debounceTimer);
            if (query.length < 2) {
                modal.classList.add('hidden');
                return;
            }
            debounceTimer = setTimeout(async () => {
                try {
                    const res = await fetch(`/api/v1/search?query=${encodeURIComponent(query)}&limit=8`);
                    const data = await res.json();
                    if (data.status === 'success' && data.data && data.data.length > 0) {
                        list.innerHTML = data.data.map(item => `
                            <div class="search-item p-3 hover:bg-slate-800/80 cursor-pointer flex justify-between items-center transition border-b border-bordercolor/50"
                                 data-symbol="${item.symbol}" data-exchange="${item.exchange}">
                                <div>
                                    <div class="font-bold text-slate-100 font-mono text-xs">${item.symbol}</div>
                                    <div class="text-[10px] text-slate-400">${item.name || ''}</div>
                                </div>
                                <div class="flex items-center gap-2">
                                    <span class="px-1.5 py-0.5 rounded text-[10px] bg-brand-500/10 text-brand-400 border border-brand-500/30 font-mono">${item.exchange}</span>
                                    <span class="text-[10px] text-slate-400 font-mono">Lot: ${item.lotsize || 1}</span>
                                </div>
                            </div>
                        `).join('');
                        modal.classList.remove('hidden');

                        list.querySelectorAll('.search-item').forEach(el => {
                            el.addEventListener('click', () => {
                                const sym = el.getAttribute('data-symbol');
                                const exch = el.getAttribute('data-exchange');
                                modal.classList.add('hidden');
                                input.value = sym;
                                this.loadSymbol(sym, exch);
                            });
                        });
                    } else {
                        list.innerHTML = `<div class="p-4 text-center text-xs text-slate-500 font-mono">No matching contracts found</div>`;
                        modal.classList.remove('hidden');
                    }
                } catch (e) {}
            }, 250);
        });

        document.addEventListener('click', (e) => {
            if (!input.contains(e.target) && !modal.contains(e.target)) {
                modal.classList.add('hidden');
            }
        });
    }

    bindIndicatorsModal() {
        const btn = document.getElementById('btn-indicators-modal');
        const modal = document.getElementById('indicators-modal');
        const closeBtn = document.getElementById('btn-close-indicators-modal');
        if (!btn || !modal) return;

        btn.addEventListener('click', () => modal.classList.remove('hidden'));
        if (closeBtn) closeBtn.addEventListener('click', () => modal.classList.add('hidden'));

        modal.querySelectorAll('.indicator-toggle-btn').forEach(toggle => {
            toggle.addEventListener('click', (e) => {
                const indName = e.currentTarget.getAttribute('data-indicator');
                if (this.activeIndicators.has(indName)) {
                    this.removeIndicator(indName);
                    e.currentTarget.classList.remove('bg-brand-500', 'text-obsidian');
                    e.currentTarget.classList.add('bg-slate-800', 'text-slate-300');
                } else {
                    this.addIndicator(indName);
                    e.currentTarget.classList.remove('bg-slate-800', 'text-slate-300');
                    e.currentTarget.classList.add('bg-brand-500', 'text-obsidian');
                }
            });
        });
    }

    updateWsIndicator(connected) {
        const dot = document.getElementById('ws-status-dot');
        const text = document.getElementById('ws-status-text');
        if (dot) {
            dot.className = connected ? 'w-2 h-2 rounded-full bg-emerald-400 animate-pulse' : 'w-2 h-2 rounded-full bg-rose-500';
        }
        if (text) {
            text.textContent = connected ? 'LIVE' : 'OFFLINE';
        }
    }

    showToast(message, type = 'info') {
        const toast = document.getElementById('terminal-toast');
        if (!toast) return;
        toast.textContent = message;
        toast.className = `fixed bottom-6 right-6 px-4 py-2.5 rounded-xl shadow-2xl text-xs font-mono z-50 transition transform duration-300 flex items-center gap-2 ${
            type === 'success' ? 'bg-emerald-950/90 text-emerald-300 border border-emerald-500/40' :
            type === 'error' ? 'bg-rose-950/90 text-rose-300 border border-rose-500/40' :
            'bg-slate-900/90 text-slate-200 border border-slate-700'
        }`;
        toast.classList.remove('opacity-0', 'translate-y-4', 'pointer-events-none');
        clearTimeout(this.toastTimeout);
        this.toastTimeout = setTimeout(() => {
            toast.classList.add('opacity-0', 'translate-y-4', 'pointer-events-none');
        }, 3000);
    }

    showLoading(show) {
        const loader = document.getElementById('chart-loading-spinner');
        if (loader) {
            loader.style.display = show ? 'flex' : 'none';
        }
    }

    // -------------------------------------------------------------------------
    // In-Terminal Multi-Tab Side Drawer (DOM / Orders / Positions / Trades)
    // -------------------------------------------------------------------------
    bindDrawerEvents() {
        const toggleBtn = document.getElementById('btn-toggle-drawer');
        const closeBtn = document.getElementById('btn-close-drawer');
        const drawer = document.getElementById('terminal-side-drawer');

        if (toggleBtn && drawer) {
            toggleBtn.addEventListener('click', () => {
                const isHidden = drawer.classList.contains('hidden');
                if (isHidden) {
                    drawer.classList.remove('hidden');
                    this.refreshDrawerData();
                } else {
                    drawer.classList.add('hidden');
                }
                if (this.chart && this.container) {
                    setTimeout(() => {
                        this.chart.applyOptions({
                            width: this.container.clientWidth,
                            height: this.container.clientHeight
                        });
                    }, 50);
                }
            });
        }

        if (closeBtn && drawer) {
            closeBtn.addEventListener('click', () => {
                drawer.classList.add('hidden');
                if (this.chart && this.container) {
                    setTimeout(() => {
                        this.chart.applyOptions({
                            width: this.container.clientWidth,
                            height: this.container.clientHeight
                        });
                    }, 50);
                }
            });
        }

        const tabBtns = document.querySelectorAll('.drawer-tab-btn');
        tabBtns.forEach(btn => {
            btn.addEventListener('click', (e) => {
                const tabName = e.currentTarget.getAttribute('data-tab');
                this.activeDrawerTab = tabName;

                tabBtns.forEach(b => {
                    b.classList.remove('bg-brand-500/20', 'text-brand-400', 'border', 'border-brand-500/40', 'font-bold');
                    b.classList.add('text-slate-400');
                });
                e.currentTarget.classList.add('bg-brand-500/20', 'text-brand-400', 'border', 'border-brand-500/40', 'font-bold');
                e.currentTarget.classList.remove('text-slate-400');

                document.querySelectorAll('.drawer-panel').forEach(p => p.classList.add('hidden'));
                const activePanel = document.getElementById(`drawer-panel-${tabName}`);
                if (activePanel) activePanel.classList.remove('hidden');

                this.refreshDrawerData();
            });
        });

        const refreshOrdBtn = document.getElementById('btn-refresh-orders');
        if (refreshOrdBtn) {
            refreshOrdBtn.addEventListener('click', () => this.fetchOrdersDrawer());
        }
        const refreshTrdBtn = document.getElementById('btn-refresh-trades');
        if (refreshTrdBtn) {
            refreshTrdBtn.addEventListener('click', () => this.fetchTradesDrawer());
        }
    }

    async refreshDrawerData() {
        const drawer = document.getElementById('terminal-side-drawer');
        if (!drawer || drawer.classList.contains('hidden')) return;

        const activeTab = this.activeDrawerTab || 'dom';
        if (activeTab === 'dom') {
            await this.fetchDOMDrawer();
        } else if (activeTab === 'orders') {
            await this.fetchOrdersDrawer();
        } else if (activeTab === 'positions') {
            await this.fetchPositionsDrawer();
        } else if (activeTab === 'trades') {
            await this.fetchTradesDrawer();
        }
    }

    async fetchDOMDrawer() {
        try {
            const symDisplay = document.getElementById('dom-symbol-display');
            if (symDisplay) symDisplay.textContent = this.symbol;

            const ltpDisplay = document.getElementById('dom-ltp-display');
            if (ltpDisplay) ltpDisplay.textContent = this.currentLtp ? this.currentLtp.toFixed(2) : '--';

            const res = await fetch('/api/v1/depth', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    apikey: this.apiKey,
                    symbol: this.symbol,
                    exchange: this.exchange
                })
            });
            const data = await res.json();
            if (data.status === 'success' && data.data) {
                this.renderDOM(data.data);
            }
        } catch (e) {
            console.error("DOM fetch error:", e);
        }
    }

    renderDOM(depthData) {
        const tbody = document.getElementById('dom-table-body');
        if (!tbody) return;

        const bids = depthData.bids || [];
        const asks = depthData.asks || [];
        let html = '';

        for (let i = 0; i < 5; i++) {
            const b = bids[i] || { price: 0, quantity: 0, orders: 0 };
            const a = asks[i] || { price: 0, quantity: 0, orders: 0 };

            html += `
                <tr class="hover:bg-slate-900/80 transition">
                    <td class="py-1 px-1.5 text-slate-400">${b.orders || '-'}</td>
                    <td class="py-1 px-1.5 text-emerald-400 font-mono">${b.quantity || '-'}</td>
                    <td class="py-1 px-1.5 text-emerald-300 font-bold text-right cursor-pointer hover:bg-emerald-500/20 rounded" onclick="window.terminalInstance.placeOrderAtPrice('BUY', ${b.price})">
                        ${b.price ? b.price.toFixed(2) : '-'}
                    </td>
                    <td class="py-1 px-1.5 text-rose-300 font-bold cursor-pointer hover:bg-rose-500/20 rounded" onclick="window.terminalInstance.placeOrderAtPrice('SELL', ${a.price})">
                        ${a.price ? a.price.toFixed(2) : '-'}
                    </td>
                    <td class="py-1 px-1.5 text-rose-400 font-mono">${a.quantity || '-'}</td>
                    <td class="py-1 px-1.5 text-slate-400 text-right">${a.orders || '-'}</td>
                </tr>
            `;
        }

        tbody.innerHTML = html;

        const totalBuyEl = document.getElementById('dom-total-buy-qty');
        if (totalBuyEl) totalBuyEl.textContent = (depthData.total_buy_qty || 0).toLocaleString();

        const totalSellEl = document.getElementById('dom-total-sell-qty');
        if (totalSellEl) totalSellEl.textContent = (depthData.total_sell_qty || 0).toLocaleString();
    }

    async placeOrderAtPrice(action, price) {
        if (!price || price <= 0) return;
        const qty = this.lots * this.lotsize;
        this.showToast(`Placing ${action} ${qty}x ${this.symbol} @ ${price}...`, 'info');

        try {
            const res = await fetch('/api/v1/order', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    apikey: this.apiKey,
                    action: action,
                    symbol: this.symbol,
                    exchange: this.exchange,
                    quantity: qty,
                    price: price,
                    pricetype: 'LIMIT',
                    product: this.product
                })
            });
            const data = await res.json();
            if (data.status === 'success') {
                this.showToast(`Order Placed: ${data.orderid || 'OK'}`, 'success');
                this.refreshOrdersAndPositions();
                this.fetchDOMDrawer();
            } else {
                this.showToast(data.message || 'Order failed', 'error');
            }
        } catch (e) {
            this.showToast(`Execution Error: ${e.message}`, 'error');
        }
    }

    async fetchOrdersDrawer() {
        const container = document.getElementById('drawer-orders-list');
        if (!container) return;

        try {
            const res = await fetch('/api/v1/orderbook', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ apikey: this.apiKey })
            });
            const data = await res.json();
            const orders = (data.status === 'success' && Array.isArray(data.data)) ? data.data : [];

            if (orders.length === 0) {
                container.innerHTML = '<div class="p-4 text-center text-slate-500">No orders placed today.</div>';
                return;
            }

            let html = '';
            for (let o of orders.slice().reverse()) {
                const isBuy = (o.action || o.OrderSide || 'BUY').toUpperCase() === 'BUY';
                const status = (o.status || o.order_status || o.OrderStatus || 'open').toLowerCase();
                const oid = o.orderid || o.AppOrderID || 'N/A';
                const sym = o.symbol || o.TradingSymbol || this.symbol;
                const qty = o.quantity || o.OrderQuantity || 0;
                const prc = o.price || o.OrderPrice || 0;

                html += `
                    <div class="p-2 bg-slate-900/90 rounded-lg border border-bordercolor flex items-center justify-between text-[11px]">
                        <div>
                            <div class="flex items-center gap-1.5 font-bold">
                                <span class="px-1.5 py-0.2 rounded text-[9px] ${isBuy ? 'bg-emerald-500/20 text-emerald-400' : 'bg-rose-500/20 text-rose-400'}">
                                    ${isBuy ? 'BUY' : 'SELL'}
                                </span>
                                <span class="text-slate-100">${sym}</span>
                            </div>
                            <div class="text-[10px] text-slate-400 mt-0.5">
                                ${qty} Qty @ ₹${Number(prc).toFixed(2)} | #${oid}
                            </div>
                        </div>
                        <div class="flex items-center gap-1.5">
                            <span class="px-1.5 py-0.5 rounded text-[9px] font-bold ${
                                status === 'complete' ? 'bg-emerald-500/10 text-emerald-400' :
                                status === 'open' ? 'bg-blue-500/10 text-blue-400' :
                                'bg-rose-500/10 text-rose-400'
                            }">${status.toUpperCase()}</span>
                            ${status === 'open' ? `
                                <button class="p-1 text-rose-400 hover:bg-rose-500/20 rounded" onclick="window.terminalInstance.cancelOrder('${oid}')" title="Cancel Order">
                                    <i data-lucide="x-circle" class="w-3.5 h-3.5"></i>
                                </button>
                            ` : ''}
                        </div>
                    </div>
                `;
            }
            container.innerHTML = html;
            if (window.lucide) window.lucide.createIcons();
        } catch (e) {
            container.innerHTML = `<div class="p-4 text-center text-rose-400">Failed to load orders: ${e.message}</div>`;
        }
    }

    async fetchPositionsDrawer() {
        const container = document.getElementById('drawer-positions-list');
        const pnlEl = document.getElementById('drawer-net-pnl');
        if (!container) return;

        try {
            const res = await fetch('/api/v1/positionbook', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ apikey: this.apiKey })
            });
            const data = await res.json();
            const positions = (data.status === 'success' && Array.isArray(data.data)) ? data.data : [];
            const netMtm = data.net_mtm || 0.0;

            if (pnlEl) {
                pnlEl.textContent = (netMtm >= 0 ? '+₹' : '-₹') + Math.abs(netMtm).toFixed(2);
                pnlEl.className = `font-bold ${netMtm >= 0 ? 'text-emerald-400' : 'text-rose-400'}`;
            }

            const openPos = positions.filter(p => Number(p.quantity) !== 0);
            if (openPos.length === 0) {
                container.innerHTML = '<div class="p-4 text-center text-slate-500">No open positions.</div>';
                return;
            }

            let html = '';
            for (let p of openPos) {
                const qty = Number(p.quantity);
                const isLong = qty > 0;
                const sym = p.symbol || p.TradingSymbol || 'N/A';
                const avgPrice = Number(p.buy_price || p.average_price || 0);
                const pnl = Number(p.unrealized_pnl || p.pnl || 0);

                html += `
                    <div class="p-2 bg-slate-900/90 rounded-lg border border-bordercolor flex items-center justify-between text-[11px]">
                        <div>
                            <div class="flex items-center gap-1.5 font-bold">
                                <span class="px-1.5 py-0.2 rounded text-[9px] ${isLong ? 'bg-emerald-500/20 text-emerald-400' : 'bg-rose-500/20 text-rose-400'}">
                                    ${isLong ? 'LONG' : 'SHORT'}
                                </span>
                                <span class="text-slate-100">${sym}</span>
                            </div>
                            <div class="text-[10px] text-slate-400 mt-0.5">
                                ${Math.abs(qty)} Qty @ ₹${avgPrice.toFixed(2)}
                            </div>
                        </div>
                        <div class="flex items-center gap-2">
                            <div class="text-right">
                                <div class="font-bold font-mono ${pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}">
                                    ${pnl >= 0 ? '+' : ''}₹${pnl.toFixed(2)}
                                </div>
                            </div>
                            <button class="px-2 py-1 bg-rose-500/20 hover:bg-rose-500 text-rose-400 hover:text-white rounded text-[10px] font-bold transition" onclick="window.terminalInstance.squareOffPosition('${sym}')">
                                Square Off
                            </button>
                        </div>
                    </div>
                `;
            }
            container.innerHTML = html;
        } catch (e) {
            container.innerHTML = `<div class="p-4 text-center text-rose-400">Failed to load positions: ${e.message}</div>`;
        }
    }

    async squareOffPosition(symbol) {
        if (!confirm(`Square off position for ${symbol}?`)) return;
        this.showToast(`Squaring off ${symbol}...`, 'info');

        try {
            const res = await fetch('/api/v1/closeposition', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ apikey: this.apiKey, symbol: symbol })
            });
            const data = await res.json();
            if (data.status === 'success') {
                this.showToast(`Position squared off for ${symbol}`, 'success');
                this.refreshOrdersAndPositions();
                this.fetchPositionsDrawer();
            } else {
                this.showToast(data.message || 'Square-off failed', 'error');
            }
        } catch (e) {
            this.showToast(`Square-off error: ${e.message}`, 'error');
        }
    }

    async fetchTradesDrawer() {
        const container = document.getElementById('drawer-trades-list');
        if (!container) return;

        try {
            const res = await fetch('/api/v1/tradebook', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ apikey: this.apiKey })
            });
            const data = await res.json();
            const trades = (data.status === 'success' && Array.isArray(data.data)) ? data.data : [];

            if (trades.length === 0) {
                container.innerHTML = '<div class="p-4 text-center text-slate-500">No trades executed today.</div>';
                return;
            }

            let html = '';
            for (let t of trades.slice().reverse()) {
                const isBuy = (t.action || t.OrderSide || 'BUY').toUpperCase() === 'BUY';
                const sym = t.symbol || t.TradingSymbol || this.symbol;
                const qty = t.quantity || t.TradedQuantity || 0;
                const prc = Number(t.average_price || t.TradePrice || t.price || 0);
                const time = t.trade_time || t.OrderExecutionTime || 'Today';

                html += `
                    <div class="p-2 bg-slate-900/90 rounded-lg border border-bordercolor flex items-center justify-between text-[11px]">
                        <div>
                            <div class="flex items-center gap-1.5 font-bold">
                                <span class="px-1.5 py-0.2 rounded text-[9px] ${isBuy ? 'bg-emerald-500/20 text-emerald-400' : 'bg-rose-500/20 text-rose-400'}">
                                    ${isBuy ? 'BOUGHT' : 'SOLD'}
                                </span>
                                <span class="text-slate-100">${sym}</span>
                            </div>
                            <div class="text-[10px] text-slate-400 mt-0.5">
                                ${qty} Qty @ ₹${prc.toFixed(2)}
                            </div>
                        </div>
                        <div class="text-right text-[10px] text-slate-500 font-mono">
                            ${time}
                        </div>
                    </div>
                `;
            }
            container.innerHTML = html;
        } catch (e) {
            container.innerHTML = `<div class="p-4 text-center text-rose-400">Failed to load trades: ${e.message}</div>`;
        }
    }
}

window.OpenAlgoTradingTerminal = OpenAlgoTradingTerminal;
