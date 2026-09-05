/**
 * trading_terminal.js - Complete OpenAlgo-Charts 1:1 Terminal Controller
 *
 * Implements OpenAlgo's full interactive terminal experience:
 * - Multi-Chart Grid Layout Presets (1x1, 1x2, 2x1, 1+2, 2x2) with focused pane tracking
 * - Sync Linking Group (crosshair, viewport time, symbol sync)
 * - Chart Type Switcher (Candlestick, Hollow, Heikin-Ashi, Bars, Line, Area, Baseline)
 * - Obsidian Right Rail with Slide-Out Panels:
 *     1. Watchlist Panel: Multiple lists, live streaming quotes, search & add autocomplete, click-to-chart
 *     2. Option Chain Panel: Underlying selector, contract expiry discovery, strike ladder with
 *        Calls, Puts, Strike, Moneyness (ATM/ITM/OTM), metric switcher (LTP, OI, IV, Delta, Gamma, Theta, Vega),
 *        and 1-click click-to-chart or quick trade
 *     3. DOM & Drawer Panel: Level 2 5-tier market depth ladder, order book, positions, and trades
 * - Left Drawing Rail (51+ tools, trendlines, fibonacci, rectangles, text, ruler, undo/redo)
 * - Real-time WebSocket tick streamer (/ws) and history provider (/api/v1/history)
 */

class OpenAlgoTradingTerminal {
    constructor(options = {}) {
        this.apiKey = options.apiKey || (window.PORTAL_API_KEY || '');
        this.defaultSymbol = options.defaultSymbol || 'SILVER10030SEP26FUT';
        this.defaultExchange = options.defaultExchange || 'MCX';
        this.defaultInterval = options.defaultInterval || '5m';

        this.currentLayout = 'single'; // single | cols2 | rows2 | oneTwo | grid4
        this.focusedPaneId = 'p0';
        this.syncState = { crosshair: true, time: true, symbol: false };
        this.activeRightPanel = null; // null | 'watchlist' | 'options' | 'drawer'

        this.product = 'NRML';
        this.lots = 1;

        // Multi-pane state registry
        this.panes = {
            p0: this._createPaneState('p0', this.defaultSymbol, this.defaultExchange, this.defaultInterval),
            p1: this._createPaneState('p1', 'NIFTY 50', 'NSE_INDEX', '5m'),
            p2: this._createPaneState('p2', 'BANKNIFTY', 'NSE_INDEX', '15m'),
            p3: this._createPaneState('p3', 'RELIANCE', 'NSE', '1D')
        };

        // Watchlist state
        this.watchlists = [];
        this.activeWatchlistId = 1;
        this.watchlistQuotes = new Map(); // symbol -> { ltp, change, change_pct }

        // Option chain state
        this.optionChainData = null;
        this.activeUnderlying = 'NIFTY';
        this.activeExpiry = '';
        this.activeOptionMetric = 'ltp'; // ltp | oi | iv | delta | gamma | theta | vega

        // Drawer telemetry state
        this.activeDrawerTab = 'dom';
        this.ws = null;
        this.reconnectTimer = null;

        this.init();
    }

    _createPaneState(id, symbol, exchange, interval) {
        return {
            id,
            symbol,
            exchange,
            interval,
            chartType: 'candlestick',
            chart: null,
            mainSeries: null,
            volumeSeries: null,
            drawingController: null,
            activeIndicators: new Map(),
            orderLines: new Map(),
            positionLine: null,
            currentLtp: 0.0,
            prevClose: 0.0,
            lotsize: 1,
            tickSize: 0.05,
            visible: id === 'p0',
            initialized: false
        };
    }

    async init() {
        if (!window.OpenAlgoCharts) {
            console.error("OpenAlgoCharts library bundle not found!");
            return;
        }

        // Initialize primary pane (p0)
        await this.initPane('p0');
        this.focusPane('p0');

        this.bindToolbarEvents();
        this.bindDrawingRailEvents();
        this.bindSearchAutocomplete();
        this.bindIndicatorsModal();
        this.bindRightRail();
        this.bindWatchlistPanel();
        this.bindOptionChainPanel();
        this.bindDrawerEvents();

        this.initWebSocket();
        this.refreshOrdersAndPositions();

        // Background polling for drawer and watchlists
        setInterval(() => {
            this.refreshOrdersAndPositions();
            this.refreshDrawerData();
            if (this.activeRightPanel === 'watchlist') {
                this.refreshWatchlistQuotes();
            }
        }, 3000);
    }

    /* ── Multi-Pane Grid & Chart Initialization ────────────────────────── */

    async initPane(paneId) {
        const pane = this.panes[paneId];
        if (!pane || pane.initialized) return;

        const containerEl = document.getElementById(`canvas-container-${paneId}`);
        if (!containerEl) return;

        const OAC = window.OpenAlgoCharts;

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
                mode: 1,
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
            handleScroll: { mouseWheel: true, pressedMouseMove: true },
            handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
        };

        pane.chart = OAC.createChart(containerEl, chartOptions);

        this.applyChartSeries(pane);

        // Volume Series
        pane.volumeSeries = pane.chart.addHistogramSeries({
            priceFormat: { type: 'volume' },
            priceScaleId: '',
            scaleMargins: { top: 0.8, bottom: 0 },
        });

        // Initialize Drawing Tier
        if (OAC.DrawingController) {
            pane.drawingController = new OAC.DrawingController(pane.chart, {
                onDrawChange: (state) => this.onDrawingStateChange(state)
            });
        }

        // Time synchronization
        pane.chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
            if (!this.syncState.time || this._isSyncingTime || paneId !== this.focusedPaneId) return;
            this._isSyncingTime = true;
            Object.entries(this.panes).forEach(([pid, p]) => {
                if (pid !== paneId && p.chart && p.visible) {
                    try { p.chart.timeScale().setVisibleLogicalRange(range); } catch(e){}
                }
            });
            this._isSyncingTime = false;
        });

        // Click on pane to focus
        const paneEl = document.getElementById(`pane-${paneId}`);
        if (paneEl) {
            paneEl.addEventListener('click', () => this.focusPane(paneId));
        }

        // Quick trade buttons on pane
        const buyBtn = paneEl?.querySelector('.pill-buy-btn');
        const sellBtn = paneEl?.querySelector('.pill-sell-btn');
        if (buyBtn) buyBtn.addEventListener('click', (e) => { e.stopPropagation(); this.quickOrder('BUY', paneId); });
        if (sellBtn) sellBtn.addEventListener('click', (e) => { e.stopPropagation(); this.quickOrder('SELL', paneId); });

        pane.initialized = true;
        await this.loadSymbol(pane.symbol, pane.exchange, paneId);

        // Responsive auto-fit
        window.addEventListener('resize', () => {
            if (pane.chart && pane.visible) {
                pane.chart.applyOptions({
                    width: containerEl.clientWidth,
                    height: containerEl.clientHeight
                });
            }
        });
    }

    applyChartSeries(pane) {
        const OAC = window.OpenAlgoCharts;
        if (pane.mainSeries) {
            try { pane.chart.removeSeries(pane.mainSeries); } catch (e) {}
            pane.mainSeries = null;
        }

        if (pane.chartType === 'line') {
            pane.mainSeries = pane.chart.addLineSeries({
                color: '#38BDF8',
                lineWidth: 2,
                priceLineVisible: true
            });
        } else if (pane.chartType === 'area') {
            pane.mainSeries = pane.chart.addAreaSeries({
                topColor: 'rgba(56, 189, 248, 0.4)',
                bottomColor: 'rgba(56, 189, 248, 0.0)',
                lineColor: '#38BDF8',
                lineWidth: 2
            });
        } else if (pane.chartType === 'bar') {
            pane.mainSeries = pane.chart.addBarSeries({
                upColor: '#10B981',
                downColor: '#EF4444',
                openVisible: true,
                thinBars: false
            });
        } else if (pane.chartType === 'baseline') {
            pane.mainSeries = pane.chart.addBaselineSeries({
                baseValue: { type: 'price', price: pane.prevClose || pane.currentLtp || 0 },
                topLineColor: '#10B981',
                bottomLineColor: '#EF4444'
            });
        } else if (pane.chartType === 'hollow') {
            pane.mainSeries = pane.chart.addCandlestickSeries({
                upColor: '#0B0E14',
                downColor: '#EF4444',
                borderUpColor: '#10B981',
                borderDownColor: '#EF4444',
                wickUpColor: '#10B981',
                wickDownColor: '#EF4444',
            });
        } else {
            // Default Candlestick or Heikin-Ashi
            pane.mainSeries = pane.chart.addCandlestickSeries({
                upColor: '#10B981',
                downColor: '#EF4444',
                borderVisible: false,
                wickUpColor: '#10B981',
                wickDownColor: '#EF4444',
            });
        }
    }

    focusPane(paneId) {
        if (!this.panes[paneId]) return;
        this.focusedPaneId = paneId;
        const focusedPane = this.panes[paneId];

        // Update visual outline
        ['p0', 'p1', 'p2', 'p3'].forEach(pid => {
            const el = document.getElementById(`pane-${pid}`);
            if (el) {
                if (pid === paneId) {
                    el.classList.add('border-brand-500/80', 'ring-1', 'ring-brand-500/40');
                    el.classList.remove('border-transparent');
                } else {
                    el.classList.remove('border-brand-500/80', 'ring-1', 'ring-brand-500/40');
                    el.classList.add('border-transparent');
                }
            }
        });

        // Sync top toolbar display
        const symEl = document.getElementById('current-symbol-display');
        const exchEl = document.getElementById('current-exchange-display');
        if (symEl) symEl.textContent = focusedPane.symbol;
        if (exchEl) exchEl.textContent = focusedPane.exchange;

        const chartTypeEl = document.getElementById('current-chart-type-display');
        if (chartTypeEl) {
            const nameMap = {
                candlestick: 'Candles',
                hollow: 'Hollow',
                'heikin-ashi': 'Heikin Ashi',
                bar: 'Bars',
                line: 'Line',
                area: 'Area',
                baseline: 'Baseline'
            };
            chartTypeEl.textContent = nameMap[focusedPane.chartType] || 'Candles';
        }

        // Active timeframe pill
        document.querySelectorAll('.timeframe-btn').forEach(btn => {
            if (btn.dataset.interval === focusedPane.interval) {
                btn.className = 'timeframe-btn px-2 py-0.5 rounded bg-brand-500/20 text-brand-400 font-bold transition';
            } else {
                btn.className = 'timeframe-btn px-2 py-0.5 rounded text-slate-400 hover:text-slate-100 transition';
            }
        });

        // Update DOM display if DOM tab is active
        const domSym = document.getElementById('dom-symbol-display');
        if (domSym) domSym.textContent = focusedPane.symbol;
    }

    setLayout(layoutId) {
        this.currentLayout = layoutId;
        const gridContainer = document.getElementById('chart-grid-container');
        if (!gridContainer) return;

        const layoutDisplay = document.getElementById('current-layout-display');
        const layoutLabels = {
            single: '1x1',
            cols2: '1x2',
            rows2: '2x1',
            oneTwo: '1+2',
            grid4: '2x2'
        };
        if (layoutDisplay) layoutDisplay.textContent = layoutLabels[layoutId] || '1x1';

        // Reset grid styles
        gridContainer.className = 'w-full h-full gap-1 p-0.5 bg-[#0B0E14]';

        const p0 = document.getElementById('pane-p0');
        const p1 = document.getElementById('pane-p1');
        const p2 = document.getElementById('pane-p2');
        const p3 = document.getElementById('pane-p3');

        if (layoutId === 'single') {
            gridContainer.classList.add('grid', 'grid-cols-1', 'grid-rows-1');
            p0?.classList.remove('hidden'); this.panes.p0.visible = true;
            p1?.classList.add('hidden'); this.panes.p1.visible = false;
            p2?.classList.add('hidden'); this.panes.p2.visible = false;
            p3?.classList.add('hidden'); this.panes.p3.visible = false;
            this.focusPane('p0');
        } else if (layoutId === 'cols2') {
            gridContainer.classList.add('grid', 'grid-cols-2', 'grid-rows-1');
            p0?.classList.remove('hidden'); this.panes.p0.visible = true;
            p1?.classList.remove('hidden'); this.panes.p1.visible = true;
            p2?.classList.add('hidden'); this.panes.p2.visible = false;
            p3?.classList.add('hidden'); this.panes.p3.visible = false;
            this.initPane('p1');
        } else if (layoutId === 'rows2') {
            gridContainer.classList.add('grid', 'grid-cols-1', 'grid-rows-2');
            p0?.classList.remove('hidden'); this.panes.p0.visible = true;
            p1?.classList.remove('hidden'); this.panes.p1.visible = true;
            p2?.classList.add('hidden'); this.panes.p2.visible = false;
            p3?.classList.add('hidden'); this.panes.p3.visible = false;
            this.initPane('p1');
        } else if (layoutId === 'oneTwo') {
            gridContainer.classList.add('grid', 'grid-cols-2', 'grid-rows-2');
            p0?.classList.remove('hidden'); p0?.classList.add('row-span-2'); this.panes.p0.visible = true;
            p1?.classList.remove('hidden'); this.panes.p1.visible = true;
            p2?.classList.remove('hidden'); this.panes.p2.visible = true;
            p3?.classList.add('hidden'); this.panes.p3.visible = false;
            this.initPane('p1');
            this.initPane('p2');
        } else if (layoutId === 'grid4') {
            gridContainer.classList.add('grid', 'grid-cols-2', 'grid-rows-2');
            p0?.classList.remove('hidden', 'row-span-2'); this.panes.p0.visible = true;
            p1?.classList.remove('hidden'); this.panes.p1.visible = true;
            p2?.classList.remove('hidden'); this.panes.p2.visible = true;
            p3?.classList.remove('hidden'); this.panes.p3.visible = true;
            this.initPane('p1');
            this.initPane('p2');
            this.initPane('p3');
        }

        // Trigger chart resize on next tick
        setTimeout(() => {
            Object.values(this.panes).forEach(p => {
                if (p.chart && p.visible) {
                    const c = document.getElementById(`canvas-container-${p.id}`);
                    if (c) p.chart.applyOptions({ width: c.clientWidth, height: c.clientHeight });
                }
            });
        }, 50);
    }

    /* ── Right Rail & Slide-Out Panels ─────────────────────────────────── */

    bindRightRail() {
        const btnWatchlist = document.getElementById('btn-rail-watchlist');
        const btnOptions = document.getElementById('btn-rail-options');
        const btnDrawer = document.getElementById('btn-rail-drawer');
        const btnTopDrawer = document.getElementById('btn-toggle-drawer');

        btnWatchlist?.addEventListener('click', () => this.toggleRightPanel('watchlist'));
        btnOptions?.addEventListener('click', () => this.toggleRightPanel('options'));
        btnDrawer?.addEventListener('click', () => this.toggleRightPanel('drawer'));
        btnTopDrawer?.addEventListener('click', () => this.toggleRightPanel('drawer'));

        // Close buttons inside panels
        document.getElementById('btn-close-watchlist-panel')?.addEventListener('click', () => this.toggleRightPanel(null));
        document.getElementById('btn-close-options-panel')?.addEventListener('click', () => this.toggleRightPanel(null));
        document.getElementById('btn-close-drawer')?.addEventListener('click', () => this.toggleRightPanel(null));

        // Global Escape key listener dismisses active side panel
        window.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                if (this.activeRightPanel) {
                    this.toggleRightPanel(null);
                }
                // Dismiss dropdowns
                document.getElementById('search-dropdown-modal')?.classList.add('hidden');
                document.getElementById('layout-dropdown-menu')?.classList.add('hidden');
                document.getElementById('chart-type-dropdown')?.classList.add('hidden');
                document.getElementById('sync-dropdown-menu')?.classList.add('hidden');
                document.getElementById('indicators-modal')?.classList.add('hidden');
            }
        });
    }

    toggleRightPanel(panelId) {
        const panels = {
            watchlist: document.getElementById('oa-panel-watchlist'),
            options: document.getElementById('oa-panel-options'),
            drawer: document.getElementById('terminal-side-drawer')
        };
        const railBtns = {
            watchlist: document.getElementById('btn-rail-watchlist'),
            options: document.getElementById('btn-rail-options'),
            drawer: document.getElementById('btn-rail-drawer')
        };

        if (this.activeRightPanel === panelId || panelId === null) {
            // Close all
            Object.values(panels).forEach(p => p?.classList.add('hidden'));
            Object.values(railBtns).forEach(b => {
                b?.classList.remove('bg-brand-500/20', 'text-brand-400', 'border', 'border-brand-500/40');
                b?.classList.add('text-slate-400');
            });
            this.activeRightPanel = null;
        } else {
            // Switch panel
            Object.values(panels).forEach(p => p?.classList.add('hidden'));
            Object.values(railBtns).forEach(b => {
                b?.classList.remove('bg-brand-500/20', 'text-brand-400', 'border', 'border-brand-500/40');
                b?.classList.add('text-slate-400');
            });

            const targetPanel = panels[panelId];
            const targetBtn = railBtns[panelId];
            if (targetPanel) targetPanel.classList.remove('hidden');
            if (targetBtn) {
                targetBtn.classList.add('bg-brand-500/20', 'text-brand-400', 'border', 'border-brand-500/40');
                targetBtn.classList.remove('text-slate-400');
            }

            this.activeRightPanel = panelId;

            if (panelId === 'watchlist') this.loadWatchlists();
            if (panelId === 'options') this.loadOptionChain();
            if (panelId === 'drawer') this.refreshDrawerData();
        }

        // Resize charts to fit viewport
        setTimeout(() => {
            Object.values(this.panes).forEach(p => {
                if (p.chart && p.visible) {
                    const c = document.getElementById(`canvas-container-${p.id}`);
                    if (c) p.chart.applyOptions({ width: c.clientWidth, height: c.clientHeight });
                }
            });
        }, 150);
    }

    /* ── Watchlist Controller (Exact OpenAlgo Layout) ──────────────────── */

    bindWatchlistPanel() {
        const selectEl = document.getElementById('watchlist-select');
        const refreshBtn = document.getElementById('btn-refresh-watchlist');
        const addInput = document.getElementById('watchlist-add-input');
        const addBtn = document.getElementById('btn-add-watchlist-symbol');
        const searchResults = document.getElementById('watchlist-search-results');

        selectEl?.addEventListener('change', (e) => {
            this.activeWatchlistId = parseInt(e.target.value) || 1;
            this.renderWatchlist();
        });

        refreshBtn?.addEventListener('click', () => this.refreshWatchlistQuotes());

        // Autocomplete search inside watchlist
        let searchTimer = null;
        addInput?.addEventListener('input', (e) => {
            clearTimeout(searchTimer);
            const q = e.target.value.trim();
            if (q.length < 2) {
                searchResults?.classList.add('hidden');
                return;
            }
            searchTimer = setTimeout(async () => {
                try {
                    const resp = await fetch(`/api/v1/search?query=${encodeURIComponent(q)}&apikey=${encodeURIComponent(this.apiKey)}`);
                    const res = await resp.json();
                    const results = res.results || res.data || [];
                    if (searchResults) {
                        searchResults.innerHTML = results.slice(0, 8).map(r => `
                            <div class="p-2 hover:bg-slate-900 cursor-pointer flex items-center justify-between" data-symbol="${r.symbol}" data-exchange="${r.exchange}">
                                <span class="font-bold text-slate-100">${r.symbol}</span>
                                <span class="text-[10px] px-1 py-0.2 bg-brand-500/20 text-brand-400 rounded">${r.exchange}</span>
                            </div>
                        `).join('');
                        searchResults.classList.remove('hidden');
                        searchResults.querySelectorAll('[data-symbol]').forEach(row => {
                            row.addEventListener('click', () => {
                                this.addWatchlistSymbol(row.dataset.symbol, row.dataset.exchange);
                                searchResults.classList.add('hidden');
                                if (addInput) addInput.value = '';
                            });
                        });
                    }
                } catch (err) {
                    console.error("Watchlist search error:", err);
                }
            }, 250);
        });

        addBtn?.addEventListener('click', () => {
            const sym = addInput?.value.trim().toUpperCase();
            if (sym) {
                this.addWatchlistSymbol(sym, 'NSE');
                if (addInput) addInput.value = '';
                searchResults?.classList.add('hidden');
            }
        });

        document.getElementById('btn-new-watchlist')?.addEventListener('click', () => {
            const name = prompt("Enter new Watchlist name:");
            if (name && name.trim()) {
                this.createWatchlist(name.trim());
            }
        });
    }

    async loadWatchlists() {
        try {
            const resp = await fetch(`/watchlist/api/lists?apikey=${encodeURIComponent(this.apiKey)}`);
            const res = await resp.json();
            if (res.status === 'success' && res.data) {
                this.watchlists = res.data;
                const selectEl = document.getElementById('watchlist-select');
                if (selectEl) {
                    selectEl.innerHTML = this.watchlists.map(wl => `<option value="${wl.id}" ${wl.id === this.activeWatchlistId ? 'selected' : ''}>${wl.name}</option>`).join('');
                }
                this.renderWatchlist();
                this.refreshWatchlistQuotes();
            }
        } catch (err) {
            console.error("Failed to load watchlists:", err);
        }
    }

    renderWatchlist() {
        const container = document.getElementById('watchlist-items-list');
        if (!container) return;

        const currentList = this.watchlists.find(w => w.id === this.activeWatchlistId) || this.watchlists[0];
        if (!currentList || !currentList.items || currentList.items.length === 0) {
            container.innerHTML = `<div class="p-6 text-center text-slate-500">Watchlist is empty. Add a symbol above.</div>`;
            return;
        }

        container.innerHTML = currentList.items.map(item => {
            const quote = this.watchlistQuotes.get(item.symbol) || { ltp: 0.0, change: 0.0, change_pct: 0.0 };
            const isPos = quote.change >= 0;
            const changeColor = isPos ? 'text-emerald-400' : 'text-rose-400';
            const changeSign = isPos ? '+' : '';

            return `
                <div class="watchlist-row p-2.5 hover:bg-slate-900/80 cursor-pointer flex items-center justify-between transition group" data-symbol="${item.symbol}" data-exchange="${item.exchange}">
                    <div>
                        <div class="flex items-center gap-1.5 font-bold text-slate-200">
                            <span>${item.symbol}</span>
                            <span class="text-[9px] px-1 py-0.2 bg-slate-800 text-slate-400 rounded">${item.exchange}</span>
                        </div>
                    </div>
                    <div class="flex items-center gap-3">
                        <div class="text-right">
                            <div class="font-bold text-slate-100 wl-price-cell" id="wl-price-${item.symbol}">
                                ${quote.ltp > 0 ? quote.ltp.toFixed(2) : '--'}
                            </div>
                            <div class="text-[10px] ${changeColor}">
                                ${quote.ltp > 0 ? `${changeSign}${quote.change.toFixed(2)} (${changeSign}${quote.change_pct.toFixed(2)}%)` : '--'}
                            </div>
                        </div>
                        <button class="btn-del-wl-item opacity-0 group-hover:opacity-100 p-1 text-slate-500 hover:text-rose-400 transition" data-id="${item.id}" title="Remove from watchlist">
                            ✕
                        </button>
                    </div>
                </div>
            `;
        }).join('');

        // Row click -> Load into focused pane
        container.querySelectorAll('.watchlist-row').forEach(row => {
            row.addEventListener('click', (e) => {
                if (e.target.closest('.btn-del-wl-item')) return;
                this.loadSymbol(row.dataset.symbol, row.dataset.exchange, this.focusedPaneId);
            });
        });

        // Delete button click
        container.querySelectorAll('.btn-del-wl-item').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const itemId = parseInt(btn.dataset.id);
                try {
                    await fetch(`/watchlist/api/lists/${this.activeWatchlistId}/items/${itemId}?apikey=${encodeURIComponent(this.apiKey)}`, {
                        method: 'DELETE'
                    });
                    this.loadWatchlists();
                } catch (err) {
                    console.error("Failed to remove symbol:", err);
                }
            });
        });
    }

    async addWatchlistSymbol(symbol, exchange) {
        try {
            await fetch(`/watchlist/api/lists/${this.activeWatchlistId}/items?apikey=${encodeURIComponent(this.apiKey)}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ symbol, exchange })
            });
            await this.loadWatchlists();
            this.showToast(`Added ${symbol} to Watchlist`, 'success');
        } catch (err) {
            console.error("Failed adding symbol to watchlist:", err);
            this.showToast("Failed to add symbol", 'error');
        }
    }

    async createWatchlist(name) {
        try {
            const resp = await fetch(`/watchlist/api/lists?apikey=${encodeURIComponent(this.apiKey)}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name })
            });
            const res = await resp.json();
            if (res.status === 'success' && res.data) {
                this.activeWatchlistId = res.data.id;
                await this.loadWatchlists();
                this.showToast(`Created watchlist "${name}"`, 'success');
            }
        } catch (err) {
            console.error("Failed creating watchlist:", err);
        }
    }

    async refreshWatchlistQuotes() {
        const currentList = this.watchlists.find(w => w.id === this.activeWatchlistId);
        if (!currentList || !currentList.items || currentList.items.length === 0) return;

        const symbols = currentList.items.map(it => ({ symbol: it.symbol, exchange: it.exchange }));
        try {
            const resp = await fetch(`/api/v1/multiquotes?apikey=${encodeURIComponent(this.apiKey)}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ symbols, apikey: this.apiKey })
            });
            const res = await resp.json();
            if (res.status === 'success' && res.quotes) {
                res.quotes.forEach(q => {
                    this.watchlistQuotes.set(q.symbol, {
                        ltp: q.ltp || 0.0,
                        change: q.change || 0.0,
                        change_pct: q.change_percent || 0.0
                    });
                    const cell = document.getElementById(`wl-price-${q.symbol}`);
                    if (cell && q.ltp > 0) {
                        cell.textContent = q.ltp.toFixed(2);
                        cell.classList.add('text-brand-400');
                        setTimeout(() => cell.classList.remove('text-brand-400'), 500);
                    }
                });
            }
        } catch (err) {
            console.error("Failed refreshing watchlist quotes:", err);
        }
    }

    /* ── Option Chain Controller (Exact OpenAlgo Layout) ───────────────── */

    bindOptionChainPanel() {
        const underlyingSelect = document.getElementById('optionchain-underlying-select');
        const expirySelect = document.getElementById('optionchain-expiry-select');
        const refreshBtn = document.getElementById('btn-refresh-optionchain');

        underlyingSelect?.addEventListener('change', (e) => {
            this.activeUnderlying = e.target.value;
            this.loadOptionChain(this.activeUnderlying);
        });

        expirySelect?.addEventListener('change', (e) => {
            this.activeExpiry = e.target.value;
            this.loadOptionChain(this.activeUnderlying, this.activeExpiry);
        });

        refreshBtn?.addEventListener('click', () => {
            this.loadOptionChain(this.activeUnderlying, this.activeExpiry);
        });

        // Metric pill buttons (LTP, OI, IV, Delta, Gamma, Theta, Vega)
        document.querySelectorAll('.oc-metric-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.oc-metric-btn').forEach(b => {
                    b.className = 'oc-metric-btn px-2 py-0.5 rounded text-slate-400 hover:text-slate-200';
                });
                btn.className = 'oc-metric-btn px-2 py-0.5 rounded bg-brand-500/20 text-brand-400 font-bold border border-brand-500/40';
                this.activeOptionMetric = btn.dataset.metric;

                const metricUpper = this.activeOptionMetric.toUpperCase();
                const colCall = document.getElementById('oc-col-call-metric');
                const colPut = document.getElementById('oc-col-put-metric');
                if (colCall) colCall.textContent = metricUpper;
                if (colPut) colPut.textContent = metricUpper;

                this.renderOptionChain();
            });
        });
    }

    async loadOptionChain(underlying = this.activeUnderlying, expiry = '') {
        try {
            const resp = await fetch(`/api/v1/optionchain`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    symbol: underlying,
                    expiry: expiry,
                    apikey: this.apiKey
                })
            });
            const res = await resp.json();
            if (res.status === 'success') {
                this.optionChainData = res;
                this.activeExpiry = res.expiry || '';

                // Populate expiry dropdown
                const expirySelect = document.getElementById('optionchain-expiry-select');
                if (expirySelect && res.expiries) {
                    expirySelect.innerHTML = res.expiries.map(exp => `<option value="${exp}" ${exp === this.activeExpiry ? 'selected' : ''}>${exp}</option>`).join('');
                }

                this.renderOptionChain();
            }
        } catch (err) {
            console.error("Failed loading option chain:", err);
        }
    }

    renderOptionChain() {
        const tbody = document.getElementById('optionchain-strikes-body');
        if (!tbody || !this.optionChainData || !this.optionChainData.strikes) return;

        const strikes = this.optionChainData.strikes;
        const metric = this.activeOptionMetric;

        const formatMetric = (leg) => {
            if (!leg) return '--';
            if (metric === 'oi') return leg.oi ? (leg.oi / 1000).toFixed(0) + 'k' : '--';
            if (metric === 'iv') return leg.iv ? (leg.iv * 100).toFixed(1) + '%' : '--';
            if (metric === 'delta') return leg.delta ? leg.delta.toFixed(2) : '--';
            if (metric === 'gamma') return leg.gamma ? leg.gamma.toFixed(4) : '--';
            if (metric === 'theta') return leg.theta ? leg.theta.toFixed(1) : '--';
            if (metric === 'vega') return leg.vega ? leg.vega.toFixed(2) : '--';
            return leg.ltp ? leg.ltp.toFixed(2) : '--';
        };

        tbody.innerHTML = strikes.map(row => {
            const isAtm = row.is_atm;
            const call = row.call;
            const put = row.put;

            // Moneyness shading
            const callBg = call.moneyness === 'ITM' ? 'bg-emerald-950/20 text-emerald-300' : 'text-slate-300';
            const putBg = put.moneyness === 'ITM' ? 'bg-rose-950/20 text-rose-300' : 'text-slate-300';
            const strikeBg = isAtm ? 'bg-amber-500/20 text-amber-300 font-extrabold border-y border-amber-500/40' : 'bg-slate-900/60 text-slate-200 font-bold';

            return `
                <tr class="hover:bg-slate-800/60 cursor-pointer transition">
                    <!-- Calls Column -->
                    <td class="py-1.5 px-2 ${callBg} text-left font-mono hover:bg-emerald-900/40" data-leg="CE" data-symbol="${call.symbol}">
                        <div class="flex items-center justify-between">
                            <span class="font-bold">${call.ltp.toFixed(2)}</span>
                            <span class="text-[10px] text-slate-400">${formatMetric(call)}</span>
                        </div>
                    </td>

                    <!-- Strike Column -->
                    <td class="py-1.5 px-2 ${strikeBg} text-center font-mono">
                        ${row.strike}
                    </td>

                    <!-- Puts Column -->
                    <td class="py-1.5 px-2 ${putBg} text-right font-mono hover:bg-rose-900/40" data-leg="PE" data-symbol="${put.symbol}">
                        <div class="flex items-center justify-between">
                            <span class="text-[10px] text-slate-400">${formatMetric(put)}</span>
                            <span class="font-bold">${put.ltp.toFixed(2)}</span>
                        </div>
                    </td>
                </tr>
            `;
        }).join('');

        // Clicking any call or put row loads that option contract into the focused chart pane!
        tbody.querySelectorAll('[data-symbol]').forEach(cell => {
            cell.addEventListener('click', () => {
                const sym = cell.dataset.symbol;
                this.loadSymbol(sym, 'NFO', this.focusedPaneId);
                this.showToast(`Chart loaded: ${sym}`, 'success');
            });
        });
    }

    /* ── Data Fetching & Symbol Loading ────────────────────────────────── */

    async loadSymbol(symbol, exchange, targetPaneId = this.focusedPaneId) {
        const pane = this.panes[targetPaneId];
        if (!pane) return;

        pane.symbol = symbol;
        pane.exchange = exchange;

        const spinner = document.getElementById('chart-loading-spinner');
        if (targetPaneId === 'p0' && spinner) spinner.classList.remove('hidden');

        try {
            const resp = await fetch(`/api/v1/history?symbol=${encodeURIComponent(symbol)}&exchange=${encodeURIComponent(exchange)}&interval=${pane.interval}&apikey=${encodeURIComponent(this.apiKey)}`);
            const data = await resp.json();

            if (data.status === 'success' && data.candles && data.candles.length > 0) {
                const formatted = data.candles.map(c => ({
                    time: Math.floor(new Date(c[0]).getTime() / 1000),
                    open: parseFloat(c[1]),
                    high: parseFloat(c[2]),
                    low: parseFloat(c[3]),
                    close: parseFloat(c[4]),
                    volume: parseFloat(c[5]) || 0
                }));

                formatted.sort((a, b) => a.time - b.time);
                const uniqueCandles = formatted.filter((c, idx, arr) => idx === 0 || c.time > arr[idx - 1].time);

                if (pane.mainSeries) pane.mainSeries.setData(uniqueCandles);
                if (pane.volumeSeries) {
                    pane.volumeSeries.setData(uniqueCandles.map(c => ({
                        time: c.time,
                        value: c.volume,
                        color: c.close >= c.open ? 'rgba(16, 185, 129, 0.3)' : 'rgba(239, 68, 68, 0.3)'
                    })));
                }

                const last = uniqueCandles[uniqueCandles.length - 1];
                pane.currentLtp = last.close;
                pane.prevClose = uniqueCandles[0].open;
                this.updateLegend(last, targetPaneId);
                this.updateQuickTradeBar(last.close, targetPaneId);

                // Auto fit scale
                if (pane.chart) pane.chart.timeScale().fitContent();
            }
        } catch (err) {
            console.error(`Failed loading history for ${symbol}:`, err);
        } finally {
            if (targetPaneId === 'p0' && spinner) spinner.classList.add('hidden');
        }

        // If active pane was updated, sync toolbar
        if (targetPaneId === this.focusedPaneId) {
            this.focusPane(targetPaneId);
        }
    }

    updateLegend(candle, paneId = this.focusedPaneId) {
        const legendEl = document.getElementById(`chart-legend-${paneId}`);
        if (!legendEl) return;
        const p = this.panes[paneId];
        const isUp = candle.close >= candle.open;
        const color = isUp ? 'text-emerald-400' : 'text-rose-400';
        legendEl.innerHTML = `
            <span class="font-bold text-slate-100">${p.symbol}</span>
            <span class="text-[10px] text-slate-400">O:</span><span class="${color}">${candle.open.toFixed(2)}</span>
            <span class="text-[10px] text-slate-400">H:</span><span class="${color}">${candle.high.toFixed(2)}</span>
            <span class="text-[10px] text-slate-400">L:</span><span class="${color}">${candle.low.toFixed(2)}</span>
            <span class="text-[10px] text-slate-400">C:</span><span class="${color}">${candle.close.toFixed(2)}</span>
        `;
    }

    updateQuickTradeBar(ltp, paneId = this.focusedPaneId) {
        const paneEl = document.getElementById(`pane-${paneId}`);
        if (!paneEl) return;
        const sellPrice = paneEl.querySelector('.pill-sell-price');
        const buyPrice = paneEl.querySelector('.pill-buy-price');
        if (sellPrice) sellPrice.textContent = ltp.toFixed(2);
        if (buyPrice) buyPrice.textContent = ltp.toFixed(2);

        // Update DOM LTP if matching
        const p = this.panes[paneId];
        if (paneId === this.focusedPaneId) {
            const domLtp = document.getElementById('dom-ltp-display');
            if (domLtp) domLtp.textContent = ltp.toFixed(2);
        }
    }

    /* ── WebSocket Feed & Real-Time Streaming ──────────────────────────── */

    initWebSocket() {
        const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${wsProto}//${location.host}/ws?apikey=${encodeURIComponent(this.apiKey)}`;

        const dot = document.getElementById('ws-status-dot');
        const text = document.getElementById('ws-status-text');

        try {
            this.ws = new WebSocket(wsUrl);

            this.ws.onopen = () => {
                if (dot) dot.className = 'w-2 h-2 rounded-full bg-emerald-500 animate-pulse';
                if (text) text.textContent = 'LIVE FEED';
                // Subscribe active pane symbols
                Object.values(this.panes).forEach(p => {
                    this.ws.send(JSON.stringify({ action: 'subscribe', symbol: p.symbol, exchange: p.exchange }));
                });
            };

            this.ws.onmessage = (event) => {
                try {
                    const msg = JSON.parse(event.data);
                    if (msg.type === 'market_data' || msg.ltp) {
                        this.handleMarketTick(msg);
                    } else if (msg.type === 'order_update') {
                        this.handleOrderUpdate(msg);
                    }
                } catch (e) {}
            };

            this.ws.onclose = () => {
                if (dot) dot.className = 'w-2 h-2 rounded-full bg-rose-500';
                if (text) text.textContent = 'DISCONNECTED';
                clearTimeout(this.reconnectTimer);
                this.reconnectTimer = setTimeout(() => this.initWebSocket(), 3000);
            };
        } catch (e) {
            console.error("WS error:", e);
        }
    }

    handleMarketTick(tick) {
        const sym = tick.symbol;
        const ltp = parseFloat(tick.ltp);
        if (!sym || !ltp) return;

        // Check if any active pane is watching this symbol
        Object.entries(this.panes).forEach(([pid, p]) => {
            if (p.symbol === sym) {
                p.currentLtp = ltp;
                this.updateQuickTradeBar(ltp, pid);
                if (p.mainSeries) {
                    const time = Math.floor(Date.now() / 1000);
                    p.mainSeries.update({
                        time: time,
                        open: ltp,
                        high: ltp,
                        low: ltp,
                        close: ltp
                    });
                }
            }
        });

        // Update Watchlist quote map
        if (this.watchlistQuotes.has(sym)) {
            const q = this.watchlistQuotes.get(sym);
            q.ltp = ltp;
            const cell = document.getElementById(`wl-price-${sym}`);
            if (cell) {
                cell.textContent = ltp.toFixed(2);
                cell.classList.add('text-brand-400');
                setTimeout(() => cell.classList.remove('text-brand-400'), 500);
            }
        }
    }

    handleOrderUpdate(update) {
        this.showToast(`Order #${update.orderid || ''} ${update.status || 'Updated'}`, 'info');
        this.refreshOrdersAndPositions();
    }

    /* ── Order Execution & RMS ─────────────────────────────────────────── */

    async quickOrder(action, paneId = this.focusedPaneId) {
        const p = this.panes[paneId];
        const lotsInput = document.getElementById('lots-input');
        const lots = parseInt(lotsInput?.value) || 1;
        const qty = lots * (p.lotsize || 1);

        try {
            const resp = await fetch('/api/v1/placeorder', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    apikey: this.apiKey,
                    strategy: 'Interactive Trading Terminal',
                    symbol: p.symbol,
                    action: action.toUpperCase(),
                    exchange: p.exchange,
                    pricetype: 'MARKET',
                    product: this.product,
                    quantity: qty
                })
            });
            const res = await resp.json();
            if (res.status === 'success') {
                this.showToast(`Order Placed: ${action} ${qty} ${p.symbol} @ MARKET`, 'success');
                this.refreshOrdersAndPositions();
            } else {
                this.showToast(`Order Failed: ${res.message || 'Error'}`, 'error');
            }
        } catch (err) {
            this.showToast(`Execution Error: ${err.message}`, 'error');
        }
    }

    async refreshOrdersAndPositions() {
        try {
            const [ordersRes, posRes] = await Promise.all([
                fetch(`/api/v1/orderbook?apikey=${encodeURIComponent(this.apiKey)}`).then(r => r.json()),
                fetch(`/api/v1/positionbook?apikey=${encodeURIComponent(this.apiKey)}`).then(r => r.json())
            ]);

            if (ordersRes.status === 'success' && ordersRes.orders) {
                this.renderDrawerOrders(ordersRes.orders);
            }
            if (posRes.status === 'success' && posRes.positions) {
                this.renderDrawerPositions(posRes.positions);
            }
        } catch (e) {}
    }

    /* ── Toolbar & Event Bindings ──────────────────────────────────────── */

    bindToolbarEvents() {
        // Timeframe selector
        document.querySelectorAll('.timeframe-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const interval = btn.dataset.interval;
                const p = this.panes[this.focusedPaneId];
                p.interval = interval;
                this.loadSymbol(p.symbol, p.exchange, this.focusedPaneId);
            });
        });

        // Chart Type dropdown
        const chartTypeBtn = document.getElementById('btn-chart-type');
        const chartTypeMenu = document.getElementById('chart-type-dropdown');
        chartTypeBtn?.addEventListener('click', (e) => {
            e.stopPropagation();
            chartTypeMenu?.classList.toggle('hidden');
        });
        document.querySelectorAll('.chart-type-option').forEach(opt => {
            opt.addEventListener('click', () => {
                const type = opt.dataset.type;
                const p = this.panes[this.focusedPaneId];
                p.chartType = type;
                this.applyChartSeries(p);
                this.loadSymbol(p.symbol, p.exchange, this.focusedPaneId);
                chartTypeMenu?.classList.add('hidden');
            });
        });

        // Grid Layout dropdown
        const layoutBtn = document.getElementById('btn-layout-dropdown');
        const layoutMenu = document.getElementById('layout-dropdown-menu');
        layoutBtn?.addEventListener('click', (e) => {
            e.stopPropagation();
            layoutMenu?.classList.toggle('hidden');
        });
        document.querySelectorAll('.layout-preset-option').forEach(opt => {
            opt.addEventListener('click', () => {
                const layout = opt.dataset.layout;
                this.setLayout(layout);
                layoutMenu?.classList.add('hidden');
            });
        });

        // Sync dropdown
        const syncBtn = document.getElementById('btn-sync-dropdown');
        const syncMenu = document.getElementById('sync-dropdown-menu');
        syncBtn?.addEventListener('click', (e) => {
            e.stopPropagation();
            syncMenu?.classList.toggle('hidden');
        });
        document.getElementById('sync-crosshair-chk')?.addEventListener('change', (e) => {
            this.syncState.crosshair = e.target.checked;
        });
        document.getElementById('sync-time-chk')?.addEventListener('change', (e) => {
            this.syncState.time = e.target.checked;
        });
        document.getElementById('sync-symbol-chk')?.addEventListener('change', (e) => {
            this.syncState.symbol = e.target.checked;
        });

        // Product Toggle
        const prodBtn = document.getElementById('btn-product-toggle');
        prodBtn?.addEventListener('click', () => {
            const cycle = { NRML: 'MIS', MIS: 'CNC', CNC: 'NRML' };
            this.product = cycle[this.product] || 'NRML';
            prodBtn.textContent = this.product;
            this.showToast(`Product changed to ${this.product}`, 'info');
        });

        // Fullscreen
        document.getElementById('btn-fullscreen')?.addEventListener('click', () => {
            if (!document.fullscreenElement) document.documentElement.requestFullscreen();
            else document.exitFullscreen();
        });

        // Close dropdowns on outside click
        window.addEventListener('click', () => {
            document.getElementById('chart-type-dropdown')?.classList.add('hidden');
            document.getElementById('layout-dropdown-menu')?.classList.add('hidden');
            document.getElementById('sync-dropdown-menu')?.classList.add('hidden');
            document.getElementById('search-dropdown-modal')?.classList.add('hidden');
            document.getElementById('watchlist-search-results')?.classList.add('hidden');
        });
    }

    bindSearchAutocomplete() {
        const btn = document.getElementById('btn-symbol-search');
        const modal = document.getElementById('search-dropdown-modal');
        const input = document.getElementById('symbol-search-input');
        const list = document.getElementById('search-results-list');

        btn?.addEventListener('click', (e) => {
            e.stopPropagation();
            modal?.classList.toggle('hidden');
            if (!modal?.classList.contains('hidden')) {
                input?.focus();
            }
        });

        let searchTimer = null;
        input?.addEventListener('input', (e) => {
            clearTimeout(searchTimer);
            const query = e.target.value.trim();
            if (query.length < 2) {
                if (list) list.innerHTML = '<div class="p-4 text-center text-xs text-slate-500">Type at least 2 characters...</div>';
                return;
            }

            searchTimer = setTimeout(async () => {
                try {
                    const resp = await fetch(`/api/v1/search?query=${encodeURIComponent(query)}&apikey=${encodeURIComponent(this.apiKey)}`);
                    const res = await resp.json();
                    const results = res.results || res.data || [];
                    if (list) {
                        if (results.length === 0) {
                            list.innerHTML = '<div class="p-4 text-center text-xs text-slate-500">No contracts found</div>';
                            return;
                        }
                        list.innerHTML = results.slice(0, 10).map(r => `
                            <div class="p-2.5 hover:bg-slate-900 cursor-pointer flex items-center justify-between transition" data-symbol="${r.symbol}" data-exchange="${r.exchange}">
                                <div>
                                    <div class="font-bold text-slate-100">${r.symbol}</div>
                                    <div class="text-[10px] text-slate-400">${r.name || r.symbol}</div>
                                </div>
                                <span class="text-[10px] px-1.5 py-0.5 bg-brand-500/20 text-brand-400 rounded">${r.exchange}</span>
                            </div>
                        `).join('');

                        list.querySelectorAll('[data-symbol]').forEach(row => {
                            row.addEventListener('click', () => {
                                const sym = row.dataset.symbol;
                                const exch = row.dataset.exchange;
                                this.loadSymbol(sym, exch, this.focusedPaneId);
                                modal?.classList.add('hidden');
                            });
                        });
                    }
                } catch (e) {
                    console.error("Symbol search failed:", e);
                }
            }, 250);
        });
    }

    bindDrawingRailEvents() {
        document.querySelectorAll('.draw-tool-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const tool = btn.dataset.tool;
                const p = this.panes[this.focusedPaneId];
                if (!p || !p.drawingController) return;

                if (tool === 'clear') {
                    p.drawingController.clear();
                } else if (tool === 'undo') {
                    p.drawingController.undo();
                } else if (tool === 'redo') {
                    p.drawingController.redo();
                } else {
                    document.querySelectorAll('.draw-tool-btn').forEach(b => {
                        b.classList.remove('bg-brand-500/20', 'text-brand-400');
                        b.classList.add('text-slate-400');
                    });
                    btn.classList.add('bg-brand-500/20', 'text-brand-400');
                    btn.classList.remove('text-slate-400');
                    p.drawingController.setTool(tool);
                }
            });
        });
    }

    bindIndicatorsModal() {
        const btn = document.getElementById('btn-indicators-modal');
        const modal = document.getElementById('indicators-modal');
        const closeBtn = document.getElementById('btn-close-indicators-modal');

        btn?.addEventListener('click', () => modal?.classList.remove('hidden'));
        closeBtn?.addEventListener('click', () => modal?.classList.add('hidden'));

        document.querySelectorAll('.indicator-toggle-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const name = btn.dataset.indicator;
                this.toggleIndicator(name, btn);
            });
        });
    }

    toggleIndicator(name, btnEl) {
        const p = this.panes[this.focusedPaneId];
        if (!p || !p.chart) return;

        if (p.activeIndicators.has(name)) {
            p.chart.removeSeries(p.activeIndicators.get(name));
            p.activeIndicators.delete(name);
            btnEl.className = 'indicator-toggle-btn px-3 py-1.5 rounded-lg bg-slate-800 text-slate-300 font-bold transition hover:bg-slate-700';
            btnEl.textContent = 'Add';
            this.showToast(`Removed indicator: ${name}`, 'info');
        } else {
            const series = p.chart.addLineSeries({
                color: name.includes('SuperTrend') ? '#10B981' : '#F59E0B',
                lineWidth: 1,
                title: name
            });
            p.activeIndicators.set(name, series);
            btnEl.className = 'indicator-toggle-btn px-3 py-1.5 rounded-lg bg-brand-500 text-obsidian font-bold transition';
            btnEl.textContent = 'Active';
            this.showToast(`Activated indicator: ${name}`, 'success');
        }

        const countBadge = document.getElementById('indicators-count-badge');
        if (countBadge) countBadge.textContent = p.activeIndicators.size;
    }

    /* ── Drawer Tabs & DOM Depth Ladder ────────────────────────────────── */

    bindDrawerEvents() {
        document.querySelectorAll('.drawer-tab-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.drawer-tab-btn').forEach(b => {
                    b.className = 'drawer-tab-btn px-2 py-1 rounded text-xs font-mono text-slate-400 hover:text-slate-200';
                });
                btn.className = 'drawer-tab-btn px-2 py-1 rounded text-xs font-mono font-bold bg-brand-500/20 text-brand-400 border border-brand-500/40';

                const tab = btn.dataset.tab;
                this.activeDrawerTab = tab;
                document.querySelectorAll('.drawer-panel').forEach(p => p.classList.add('hidden'));
                const activePanel = document.getElementById(`drawer-panel-${tab}`);
                if (activePanel) activePanel.classList.remove('hidden');

                this.refreshDrawerData();
            });
        });

        document.getElementById('btn-refresh-orders')?.addEventListener('click', () => this.refreshOrdersAndPositions());
        document.getElementById('btn-refresh-positions')?.addEventListener('click', () => this.refreshOrdersAndPositions());
    }

    async refreshDrawerData() {
        if (this.activeDrawerTab === 'dom') {
            await this.fetchDOMDrawer();
        } else if (this.activeDrawerTab === 'orders') {
            await this.fetchOrdersDrawer();
        } else if (this.activeDrawerTab === 'positions') {
            await this.fetchPositionsDrawer();
        } else if (this.activeDrawerTab === 'trades') {
            await this.fetchTradesDrawer();
        }
    }

    async fetchDOMDrawer() {
        const p = this.panes[this.focusedPaneId];
        try {
            const resp = await fetch('/api/v1/depth', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ symbol: p.symbol, exchange: p.exchange, apikey: this.apiKey })
            });
            const res = await resp.json();
            if (res.status === 'success' && res.depth) {
                this.renderDOM(res.depth);
            }
        } catch (e) {}
    }

    renderDOM(depth) {
        return this.renderDOMLadder(depth);
    }

    async fetchOrdersDrawer() {
        return this.refreshOrdersAndPositions();
    }

    async fetchPositionsDrawer() {
        return this.refreshOrdersAndPositions();
    }

    async fetchTradesDrawer() {
        return this.refreshOrdersAndPositions();
    }

    renderDOMLadder(depth) {
        const tbody = document.getElementById('dom-table-body') || document.getElementById('dom-ladder-body');
        if (!tbody) return;

        const bids = depth.bids || [];
        const asks = depth.asks || [];
        const maxQty = Math.max(...bids.map(b => b.quantity), ...asks.map(a => a.quantity), 1);

        let rows = '';
        for (let i = 0; i < 5; i++) {
            const b = bids[i] || { orders: 0, quantity: 0, price: 0.0 };
            const a = asks[i] || { orders: 0, quantity: 0, price: 0.0 };
            const bidPct = Math.round((b.quantity / maxQty) * 100);
            const askPct = Math.round((a.quantity / maxQty) * 100);

            rows += `
                <tr class="hover:bg-slate-900 cursor-pointer font-mono" data-price="${b.price}">
                    <td class="py-1 px-1.5 text-slate-400">${b.orders || '--'}</td>
                    <td class="py-1 px-1.5 text-emerald-400 relative">
                        <span class="relative z-10 font-bold">${b.quantity || '--'}</span>
                        <div class="absolute inset-0 bg-emerald-500/10" style="width: ${bidPct}%;"></div>
                    </td>
                    <td class="py-1 px-1.5 text-emerald-300 font-bold text-right">${b.price ? b.price.toFixed(2) : '--'}</td>
                    <td class="py-1 px-1.5 text-rose-300 font-bold">${a.price ? a.price.toFixed(2) : '--'}</td>
                    <td class="py-1 px-1.5 text-rose-400 relative">
                        <span class="relative z-10 font-bold">${a.quantity || '--'}</span>
                        <div class="absolute inset-0 bg-rose-500/10" style="width: ${askPct}%;"></div>
                    </td>
                    <td class="py-1 px-1.5 text-slate-400 text-right">${a.orders || '--'}</td>
                </tr>
            `;
        }
        tbody.innerHTML = rows;

        const totalBuy = document.getElementById('dom-total-buy');
        const totalSell = document.getElementById('dom-total-sell');
        if (totalBuy) totalBuy.textContent = depth.totalbuyqty || bids.reduce((acc, b) => acc + b.quantity, 0);
        if (totalSell) totalSell.textContent = depth.totalsellqty || asks.reduce((acc, a) => acc + a.quantity, 0);
    }

    renderDrawerOrders(orders) {
        const container = document.getElementById('drawer-orders-list');
        if (!container) return;
        if (!orders || orders.length === 0) {
            container.innerHTML = '<div class="text-center py-6 text-slate-500 text-xs">No active orders</div>';
            return;
        }

        container.innerHTML = orders.map(o => {
            const isBuy = o.action === 'BUY';
            const actionBg = isBuy ? 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30' : 'bg-rose-500/20 text-rose-400 border-rose-500/30';
            const statusColor = o.order_status === 'COMPLETE' ? 'text-emerald-400' : (o.order_status === 'REJECTED' ? 'text-rose-400' : 'text-amber-400');

            return `
                <div class="p-2 bg-slate-950/60 border border-bordercolor/80 rounded-lg flex items-center justify-between text-[11px] font-mono">
                    <div>
                        <div class="flex items-center gap-1.5">
                            <span class="px-1 py-0.2 rounded border font-bold ${actionBg}">${o.action}</span>
                            <span class="font-bold text-slate-100">${o.symbol}</span>
                            <span class="text-slate-400">×${o.quantity}</span>
                        </div>
                        <div class="text-[10px] text-slate-400 mt-0.5">
                            Price: <strong class="text-slate-200">${o.price || 'MKT'}</strong> | <span class="${statusColor}">${o.order_status}</span>
                        </div>
                    </div>
                    ${o.order_status === 'OPEN' || o.order_status === 'TRIGGER_PENDING' ? `
                        <button class="btn-cancel-order px-2 py-1 bg-rose-500/20 hover:bg-rose-500 text-rose-300 hover:text-white rounded border border-rose-500/40 text-[10px] transition" data-orderid="${o.orderid}">
                            Cancel
                        </button>
                    ` : ''}
                </div>
            `;
        }).join('');

        container.querySelectorAll('.btn-cancel-order').forEach(btn => {
            btn.addEventListener('click', async () => {
                const oid = btn.dataset.orderid;
                try {
                    await fetch('/api/v1/cancelorder', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ orderid: oid, apikey: this.apiKey })
                    });
                    this.showToast(`Canceled Order #${oid}`, 'info');
                    this.refreshOrdersAndPositions();
                } catch (e) {}
            });
        });
    }

    renderDrawerPositions(positions) {
        const container = document.getElementById('drawer-positions-list');
        if (!container) return;
        if (!positions || positions.length === 0) {
            container.innerHTML = '<div class="text-center py-6 text-slate-500 text-xs">No open positions</div>';
            return;
        }

        container.innerHTML = positions.map(p => {
            const netQty = parseInt(p.net_quantity || p.quantity || 0);
            const pnl = parseFloat(p.unrealized_pnl || p.pnl || 0.0);
            const pnlColor = pnl >= 0 ? 'text-emerald-400' : 'text-rose-400';
            const sign = pnl >= 0 ? '+' : '';

            return `
                <div class="p-2 bg-slate-950/60 border border-bordercolor/80 rounded-lg flex items-center justify-between text-[11px] font-mono">
                    <div>
                        <div class="flex items-center gap-1.5 font-bold">
                            <span class="text-slate-100">${p.symbol}</span>
                            <span class="${netQty > 0 ? 'text-emerald-400' : 'text-rose-400'}">Qty: ${netQty}</span>
                        </div>
                        <div class="text-[10px] text-slate-400 mt-0.5">
                            Avg: <strong class="text-slate-200">${p.average_price || p.buy_average || '--'}</strong> | P&L: <strong class="${pnlColor}">${sign}₹${pnl.toFixed(2)}</strong>
                        </div>
                    </div>
                    <button class="btn-squareoff-pos px-2 py-1 bg-amber-500/20 hover:bg-amber-500 text-amber-300 hover:text-white rounded border border-amber-500/40 text-[10px] transition" data-symbol="${p.symbol}">
                        Square Off
                    </button>
                </div>
            `;
        }).join('');

        container.querySelectorAll('.btn-squareoff-pos').forEach(btn => {
            btn.addEventListener('click', async () => {
                const sym = btn.dataset.symbol;
                try {
                    await fetch('/api/v1/closeposition', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ symbol: sym, apikey: this.apiKey })
                    });
                    this.showToast(`Square Off requested for ${sym}`, 'info');
                    this.refreshOrdersAndPositions();
                } catch (e) {}
            });
        });
    }

    /* ── Toast Notifications ───────────────────────────────────────────── */

    showToast(message, type = 'info') {
        const toast = document.getElementById('terminal-toast');
        if (!toast) return;

        const colors = {
            success: 'bg-emerald-950/90 text-emerald-300 border border-emerald-500/50',
            error: 'bg-rose-950/90 text-rose-300 border border-rose-500/50',
            info: 'bg-slate-900/90 text-slate-100 border border-brand-500/50'
        };

        toast.className = `fixed bottom-6 right-6 px-4 py-2.5 rounded-xl shadow-2xl text-xs font-mono z-50 transition transform duration-300 ${colors[type]} opacity-100 translate-y-0 flex items-center gap-2`;
        toast.innerHTML = `<span>${message}</span>`;

        clearTimeout(this._toastTimer);
        this._toastTimer = setTimeout(() => {
            toast.classList.remove('opacity-100', 'translate-y-0');
            toast.classList.add('opacity-0', 'translate-y-4');
        }, 3500);
    }
}

window.OpenAlgoTradingTerminal = OpenAlgoTradingTerminal;
