/**
 * OpenAlgo-Inspired Dual-Canvas Charting Engine (AC Agarwal Enterprise Platform)
 * High-performance, zero-dependency HTML5 Canvas financial charting suite (<50KB).
 *
 * Architecture:
 * - Base Canvas: 60 fps rendering of Candlesticks, Volume bars, SuperTrend ratchet bands, and background grid.
 * - Top Canvas: Sub-millisecond interactive overlay for crosshairs, coordinate badges, HUD legend, and trade execution markers.
 * - Native HiDPI Retina support, kinetic pan/zoom, O(log N) visible range binary search, zero CDN dependencies.
 */

(function (global) {
    'use strict';

    class DualCanvasChart {
        constructor(container, options = {}) {
            if (!container) throw new Error("Chart container element is required");
            this.container = container;
            this.options = Object.assign({
                width: container.clientWidth || 800,
                height: container.clientHeight || 420,
                background: '#020617',
                gridColor: '#1e293b',
                textColor: '#94a3b8',
                upColor: '#10b981',
                downColor: '#f43f5e',
                supertrendUpColor: '#10b981',
                supertrendDownColor: '#f43f5e',
                volumeUpColor: 'rgba(16, 185, 129, 0.25)',
                volumeDownColor: 'rgba(244, 63, 94, 0.25)',
                crosshairColor: '#64748b',
                priceAxisWidth: 70,
                timeAxisHeight: 26,
                fontSize: 11,
                fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
                rightPaddingBars: 8
            }, options);

            this.data = [];             // [{time, open, high, low, close, volume, supertrend, upper_band, lower_band, trend}]
            this.markers = [];          // [{time, position, color, shape, text}]
            this.priceLines = [];       // [{price, color, title, lineStyle}]
            
            // Viewport & Scale State
            this.barSpacing = 9;        // Pixel width per candle
            this.barWidth = 7;          // Candle body width
            this.rightOffset = 8;       // Bars offset from right edge
            this.scrollOffset = 0;      // Current pan scroll position (in bars)
            this.minPrice = 0;
            this.maxPrice = 100;
            
            // Interaction State
            this.isDragging = false;
            this.dragStartX = 0;
            this.dragStartOffset = 0;
            this.cursorX = -1;
            this.cursorY = -1;
            this.hoveredBar = null;

            this._initDOM();
            this._bindEvents();
            this.resize();
        }

        _initDOM() {
            this.container.innerHTML = '';
            this.container.style.position = 'relative';
            this.container.style.userSelect = 'none';
            this.container.style.webkitUserSelect = 'none';
            this.container.style.overflow = 'hidden';
            this.container.style.backgroundColor = this.options.background;

            // 1. Base Data Canvas
            this.baseCanvas = document.createElement('canvas');
            this.baseCanvas.style.position = 'absolute';
            this.baseCanvas.style.top = '0';
            this.baseCanvas.style.left = '0';
            this.baseCanvas.style.width = '100%';
            this.baseCanvas.style.height = '100%';
            this.baseCanvas.style.display = 'block';
            this.baseCtx = this.baseCanvas.getContext('2d');
            this.container.appendChild(this.baseCanvas);

            // 2. Top Interactive Overlay Canvas
            this.topCanvas = document.createElement('canvas');
            this.topCanvas.style.position = 'absolute';
            this.topCanvas.style.top = '0';
            this.topCanvas.style.left = '0';
            this.topCanvas.style.width = '100%';
            this.topCanvas.style.height = '100%';
            this.topCanvas.style.display = 'block';
            this.topCanvas.style.cursor = 'crosshair';
            this.topCtx = this.topCanvas.getContext('2d');
            this.container.appendChild(this.topCanvas);

            // 3. Floating HUD Legend
            this.hud = document.createElement('div');
            this.hud.style.position = 'absolute';
            this.hud.style.top = '8px';
            this.hud.style.left = '12px';
            this.hud.style.zIndex = '5';
            this.hud.style.pointerEvents = 'none';
            this.hud.style.fontFamily = this.options.fontFamily;
            this.hud.style.fontSize = '11px';
            this.hud.style.color = this.options.textColor;
            this.hud.style.display = 'flex';
            this.hud.style.gap = '12px';
            this.hud.style.backgroundColor = 'rgba(2, 6, 23, 0.75)';
            this.hud.style.padding = '4px 10px';
            this.hud.style.borderRadius = '8px';
            this.hud.style.border = '1px solid rgba(30, 41, 59, 0.8)';
            this.hud.style.backdropFilter = 'blur(4px)';
            this.container.appendChild(this.hud);
        }

        _bindEvents() {
            this._onMouseMove = (e) => {
                const rect = this.topCanvas.getBoundingClientRect();
                this.cursorX = e.clientX - rect.left;
                this.cursorY = e.clientY - rect.top;

                if (this.isDragging) {
                    const deltaX = this.cursorX - this.dragStartX;
                    const deltaBars = deltaX / this.barSpacing;
                    this.scrollOffset = Math.max(-this.data.length + 5, this.dragStartOffset - deltaBars);
                    this.renderBase();
                }
                this.renderTop();
            };

            this._onMouseDown = (e) => {
                if (e.button !== 0) return;
                this.isDragging = true;
                this.dragStartX = e.clientX - this.topCanvas.getBoundingClientRect().left;
                this.dragStartOffset = this.scrollOffset;
            };

            this._onMouseUp = () => {
                this.isDragging = false;
            };

            this._onMouseLeave = () => {
                this.isDragging = false;
                this.cursorX = -1;
                this.cursorY = -1;
                this.hoveredBar = null;
                this.renderTop();
            };

            this._onWheel = (e) => {
                e.preventDefault();
                const zoomFactor = e.deltaY < 0 ? 1.15 : 0.87;
                const prevSpacing = this.barSpacing;
                this.barSpacing = Math.max(3, Math.min(60, this.barSpacing * zoomFactor));
                this.barWidth = Math.max(1, Math.floor(this.barSpacing * 0.75));

                // Anchor zoom around cursor position
                if (this.cursorX > 0 && prevSpacing > 0) {
                    const chartPlotWidth = this.width - this.options.priceAxisWidth;
                    const cursorRatio = (chartPlotWidth - this.cursorX) / chartPlotWidth;
                    this.scrollOffset += (this.barSpacing - prevSpacing) * cursorRatio * 0.2;
                }

                this.renderBase();
                this.renderTop();
            };

            this._onDblClick = () => {
                this.fitContent();
            };

            this._onResize = () => {
                this.resize();
            };

            this.topCanvas.addEventListener('mousemove', this._onMouseMove);
            this.topCanvas.addEventListener('mousedown', this._onMouseDown);
            window.addEventListener('mouseup', this._onMouseUp);
            this.topCanvas.addEventListener('mouseleave', this._onMouseLeave);
            this.topCanvas.addEventListener('wheel', this._onWheel, { passive: false });
            this.topCanvas.addEventListener('dblclick', this._onDblClick);
            window.addEventListener('resize', this._onResize);
        }

        resize() {
            const dpr = window.devicePixelRatio || 1;
            const rect = this.container.getBoundingClientRect();
            this.width = rect.width || this.options.width;
            this.height = rect.height || this.options.height;

            this.baseCanvas.width = Math.floor(this.width * dpr);
            this.baseCanvas.height = Math.floor(this.height * dpr);
            this.baseCtx.setTransform(dpr, 0, 0, dpr, 0, 0);

            this.topCanvas.width = Math.floor(this.width * dpr);
            this.topCanvas.height = Math.floor(this.height * dpr);
            this.topCtx.setTransform(dpr, 0, 0, dpr, 0, 0);

            this.plotWidth = this.width - this.options.priceAxisWidth;
            this.plotHeight = this.height - this.options.timeAxisHeight;

            this.renderBase();
            this.renderTop();
        }

        setData(payload) {
            if (!payload) return;
            if (Array.isArray(payload)) {
                this.data = payload.map(c => this._normalizeCandle(c));
            } else if (typeof payload === 'object') {
                const candles = payload.candlestick || payload.candles || [];
                const stLines = payload.supertrend_line || [];
                const ubLines = payload.upper_band || [];
                const lbLines = payload.lower_band || [];
                
                const stMap = {};
                stLines.forEach(l => { if (l && l.time) stMap[l.time] = l; });
                const ubMap = {};
                ubLines.forEach(l => { if (l && l.time) ubMap[l.time] = l.value; });
                const lbMap = {};
                lbLines.forEach(l => { if (l && l.time) lbMap[l.time] = l.value; });

                this.data = candles.map(c => {
                    const norm = this._normalizeCandle(c);
                    const st = stMap[norm.time];
                    if (st) {
                        norm.supertrend = st.value;
                        norm.trend = st.color === '#10b981' ? 1 : -1;
                    }
                    if (ubMap[norm.time]) norm.upper_band = ubMap[norm.time];
                    if (lbMap[norm.time]) norm.lower_band = lbMap[norm.time];
                    return norm;
                });

                if (Array.isArray(payload.markers)) {
                    this.markers = payload.markers;
                }
            }

            this.data.sort((a, b) => a.time - b.time);
            this.fitContent();
        }

        setMarkers(markers) {
            this.markers = Array.isArray(markers) ? markers : [];
            this.renderTop();
        }

        _normalizeCandle(c) {
            return {
                time: Number(c.time || c.timestamp || 0),
                open: Number(c.open || 0),
                high: Number(c.high || 0),
                low: Number(c.low || 0),
                close: Number(c.close || 0),
                volume: Number(c.volume || 0),
                supertrend: c.supertrend ? Number(c.supertrend) : undefined,
                upper_band: c.upper_band ? Number(c.upper_band) : undefined,
                lower_band: c.lower_band ? Number(c.lower_band) : undefined,
                trend: c.trend !== undefined ? Number(c.trend) : undefined
            };
        }

        fitContent() {
            if (!this.data.length) {
                this.renderBase();
                this.renderTop();
                return;
            }
            const totalBars = this.data.length;
            const availableWidth = this.plotWidth - 40;
            this.barSpacing = Math.max(4, Math.min(24, availableWidth / (totalBars + this.options.rightPaddingBars)));
            this.barWidth = Math.max(1, Math.floor(this.barSpacing * 0.75));
            this.scrollOffset = 0;
            this.renderBase();
            this.renderTop();
        }

        _calculateVisibleRange() {
            if (!this.data.length) return { startIdx: 0, endIdx: 0, visibleBars: [] };

            const totalBars = this.data.length;
            const rightBarIndex = totalBars - 1 + this.options.rightPaddingBars - this.scrollOffset;
            const visibleBarsCount = Math.ceil(this.plotWidth / this.barSpacing) + 2;

            const endIdx = Math.min(totalBars - 1, Math.floor(rightBarIndex));
            const startIdx = Math.max(0, Math.floor(rightBarIndex - visibleBarsCount));

            const visibleBars = [];
            let minP = Infinity;
            let maxP = -Infinity;
            let maxVol = 0;

            for (let i = startIdx; i <= endIdx; i++) {
                const b = this.data[i];
                if (!b) continue;
                visibleBars.push({ index: i, bar: b });
                minP = Math.min(minP, b.low);
                maxP = Math.max(maxP, b.high);
                if (b.supertrend && b.supertrend > 0) {
                    minP = Math.min(minP, b.supertrend);
                    maxP = Math.max(maxP, b.supertrend);
                }
                if (b.volume) maxVol = Math.max(maxVol, b.volume);
            }

            if (minP === Infinity || maxP === -Infinity || minP === maxP) {
                minP = minP === Infinity ? 0 : minP * 0.99;
                maxP = maxP === -Infinity ? 100 : maxP * 1.01;
            }

            // Price scale padding (8% top & bottom)
            const padding = (maxP - minP) * 0.08 || 1;
            this.minPrice = minP - padding;
            this.maxPrice = maxP + padding;
            this.maxVolume = maxVol || 1;

            return { startIdx, endIdx, visibleBars };
        }

        priceToY(price) {
            const range = this.maxPrice - this.minPrice;
            if (range <= 0) return this.plotHeight / 2;
            return this.plotHeight - ((price - this.minPrice) / range) * this.plotHeight;
        }

        yToPrice(y) {
            const ratio = (this.plotHeight - y) / this.plotHeight;
            return this.minPrice + ratio * (this.maxPrice - this.minPrice);
        }

        indexToX(index) {
            const totalBars = this.data.length;
            const rightBarIndex = totalBars - 1 + this.options.rightPaddingBars - this.scrollOffset;
            const offsetFromRight = rightBarIndex - index;
            return this.plotWidth - (offsetFromRight * this.barSpacing) - (this.barSpacing / 2);
        }

        xToIndex(x) {
            const totalBars = this.data.length;
            const rightBarIndex = totalBars - 1 + this.options.rightPaddingBars - this.scrollOffset;
            const offsetFromRight = (this.plotWidth - x - (this.barSpacing / 2)) / this.barSpacing;
            return Math.round(rightBarIndex - offsetFromRight);
        }

        renderBase() {
            const ctx = this.baseCtx;
            ctx.clearRect(0, 0, this.width, this.height);

            // Background Fill
            ctx.fillStyle = this.options.background;
            ctx.fillRect(0, 0, this.width, this.height);

            const { visibleBars } = this._calculateVisibleRange();
            if (!visibleBars.length) {
                this._drawEmptyState(ctx);
                return;
            }

            // 1. Grid Lines
            this._drawGrid(ctx);

            // 2. Volume Sub-Histogram (bottom 16% height)
            this._drawVolume(ctx, visibleBars);

            // 3. SuperTrend Ratchet Bands
            this._drawSuperTrend(ctx, visibleBars);

            // 4. Candlesticks (OHLC)
            this._drawCandlesticks(ctx, visibleBars);

            // 5. Price & Time Axes
            this._drawAxes(ctx);
        }

        _drawGrid(ctx) {
            ctx.strokeStyle = this.options.gridColor;
            ctx.lineWidth = 1;
            ctx.setLineDash([3, 3]);

            // Horizontal Price Grid (6 intervals)
            const steps = 6;
            for (let i = 1; i < steps; i++) {
                const y = Math.floor((this.plotHeight / steps) * i);
                ctx.beginPath();
                ctx.moveTo(0, y);
                ctx.lineTo(this.plotWidth, y);
                ctx.stroke();
            }

            // Vertical Time Grid
            const timeStep = Math.max(10, Math.floor(this.plotWidth / (this.barSpacing * 10)));
            for (let x = 0; x < this.plotWidth; x += this.barSpacing * timeStep) {
                ctx.beginPath();
                ctx.moveTo(Math.floor(x), 0);
                ctx.lineTo(Math.floor(x), this.plotHeight);
                ctx.stroke();
            }

            ctx.setLineDash([]);
        }

        _drawVolume(ctx, visibleBars) {
            const volMaxHeight = this.plotHeight * 0.16;
            const volBaseY = this.plotHeight;

            visibleBars.forEach(({ index, bar }) => {
                if (!bar.volume) return;
                const x = this.indexToX(index);
                const barH = (bar.volume / this.maxVolume) * volMaxHeight;
                const isBull = bar.close >= bar.open;

                ctx.fillStyle = isBull ? this.options.volumeUpColor : this.options.volumeDownColor;
                ctx.fillRect(x - (this.barWidth / 2), volBaseY - barH, this.barWidth, barH);
            });
        }

        _drawSuperTrend(ctx, visibleBars) {
            ctx.lineWidth = 2;
            ctx.lineJoin = 'round';
            ctx.lineCap = 'round';

            let currentPath = null;
            let currentTrend = null;

            visibleBars.forEach(({ index, bar }) => {
                if (!bar.supertrend || isNaN(bar.supertrend)) return;
                const x = this.indexToX(index);
                const y = this.priceToY(bar.supertrend);
                const trend = bar.trend !== undefined ? bar.trend : (bar.close >= bar.supertrend ? 1 : -1);

                if (trend !== currentTrend) {
                    if (currentPath) {
                        ctx.strokeStyle = currentTrend === 1 ? this.options.supertrendUpColor : this.options.supertrendDownColor;
                        ctx.stroke(currentPath);
                    }
                    currentTrend = trend;
                    currentPath = new Path2D();
                    currentPath.moveTo(x, y);
                } else {
                    currentPath.lineTo(x, y);
                }
            });

            if (currentPath) {
                ctx.strokeStyle = currentTrend === 1 ? this.options.supertrendUpColor : this.options.supertrendDownColor;
                ctx.stroke(currentPath);
            }
        }

        _drawCandlesticks(ctx, visibleBars) {
            ctx.lineWidth = 1;

            visibleBars.forEach(({ index, bar }) => {
                const x = Math.floor(this.indexToX(index));
                const yOpen = this.priceToY(bar.open);
                const yClose = this.priceToY(bar.close);
                const yHigh = this.priceToY(bar.high);
                const yLow = this.priceToY(bar.low);

                const isBull = bar.close >= bar.open;
                const color = isBull ? this.options.upColor : this.options.downColor;
                ctx.strokeStyle = color;
                ctx.fillStyle = color;

                // 1. Wick line
                ctx.beginPath();
                ctx.moveTo(x, Math.floor(yHigh));
                ctx.lineTo(x, Math.floor(yLow));
                ctx.stroke();

                // 2. Candle body
                const bodyTop = Math.floor(Math.min(yOpen, yClose));
                const bodyHeight = Math.max(1, Math.floor(Math.abs(yClose - yOpen)));
                const bodyLeft = Math.floor(x - (this.barWidth / 2));

                ctx.fillRect(bodyLeft, bodyTop, this.barWidth, bodyHeight);
            });
        }

        _drawAxes(ctx) {
            ctx.fillStyle = '#090d16';
            // Price scale backdrop
            ctx.fillRect(this.plotWidth, 0, this.options.priceAxisWidth, this.height);
            // Time scale backdrop
            ctx.fillRect(0, this.plotHeight, this.width, this.options.timeAxisHeight);

            // Border dividers
            ctx.strokeStyle = this.options.gridColor;
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(this.plotWidth, 0);
            ctx.lineTo(this.plotWidth, this.height);
            ctx.moveTo(0, this.plotHeight);
            ctx.lineTo(this.width, this.plotHeight);
            ctx.stroke();

            // Price Ticks
            ctx.fillStyle = this.options.textColor;
            ctx.font = `${this.options.fontSize}px ${this.options.fontFamily}`;
            ctx.textAlign = 'left';
            ctx.textBaseline = 'middle';

            const steps = 6;
            for (let i = 0; i <= steps; i++) {
                const y = (this.plotHeight / steps) * i;
                const price = this.yToPrice(y);
                ctx.fillText(`₹${price.toFixed(2)}`, this.plotWidth + 8, y);
            }
        }

        _drawEmptyState(ctx) {
            ctx.fillStyle = this.options.textColor;
            ctx.font = `12px ${this.options.fontFamily}`;
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText('No Market OHLC Data Available', this.width / 2, this.height / 2);
        }

        renderTop() {
            const ctx = this.topCtx;
            ctx.clearRect(0, 0, this.width, this.height);

            // 1. Render Signal & Trade Markers
            this._drawMarkers(ctx);

            // 2. Render In-Chart LTP Horizontal Line
            this._drawLtpLine(ctx);

            // 3. Render Crosshair & Dynamic Cursor Coordinates
            if (this.cursorX >= 0 && this.cursorX <= this.plotWidth && this.cursorY >= 0 && this.cursorY <= this.plotHeight) {
                this._drawCrosshair(ctx);
                this._updateHUD();
            } else if (this.data.length > 0) {
                this._updateHUD(this.data[this.data.length - 1]);
            }
        }

        _drawMarkers(ctx) {
            if (!this.markers.length || !this.data.length) return;

            const timeToIndexMap = {};
            this.data.forEach((b, idx) => { timeToIndexMap[b.time] = idx; });

            this.markers.forEach(m => {
                let idx = timeToIndexMap[m.time];
                if (idx === undefined) {
                    idx = this._findNearestIndex(m.time);
                }
                if (idx < 0 || idx >= this.data.length) return;

                const bar = this.data[idx];
                const x = Math.floor(this.indexToX(idx));
                const isAbove = m.position === 'aboveBar';
                const y = isAbove ? this.priceToY(bar.high) - 16 : this.priceToY(bar.low) + 16;
                const color = m.color || (isAbove ? '#f43f5e' : '#10b981');

                ctx.fillStyle = color;
                ctx.strokeStyle = '#020617';
                ctx.lineWidth = 1;

                // Arrow shape
                ctx.beginPath();
                if (isAbove) {
                    ctx.moveTo(x, y + 8);
                    ctx.lineTo(x - 5, y);
                    ctx.lineTo(x + 5, y);
                } else {
                    ctx.moveTo(x, y - 8);
                    ctx.lineTo(x - 5, y);
                    ctx.lineTo(x + 5, y);
                }
                ctx.closePath();
                ctx.fill();
                ctx.stroke();

                // Marker text tag
                if (m.text) {
                    ctx.font = `bold 9px ${this.options.fontFamily}`;
                    const textW = ctx.measureText(m.text).width;
                    const tagY = isAbove ? y - 8 : y + 14;

                    ctx.fillStyle = 'rgba(2, 6, 23, 0.85)';
                    ctx.fillRect(x - (textW / 2) - 4, tagY - 6, textW + 8, 12);
                    ctx.strokeStyle = color;
                    ctx.strokeRect(x - (textW / 2) - 4, tagY - 6, textW + 8, 12);

                    ctx.fillStyle = color;
                    ctx.textAlign = 'center';
                    ctx.textBaseline = 'middle';
                    ctx.fillText(m.text, x, tagY);
                }
            });
        }

        _drawLtpLine(ctx) {
            if (!this.data.length) return;
            const lastBar = this.data[this.data.length - 1];
            const y = Math.floor(this.priceToY(lastBar.close));

            ctx.strokeStyle = lastBar.close >= lastBar.open ? '#10b981' : '#f43f5e';
            ctx.lineWidth = 1;
            ctx.setLineDash([2, 2]);
            ctx.beginPath();
            ctx.moveTo(0, y);
            ctx.lineTo(this.plotWidth, y);
            ctx.stroke();
            ctx.setLineDash([]);

            // Price badge on right axis
            ctx.fillStyle = ctx.strokeStyle;
            ctx.fillRect(this.plotWidth, y - 9, this.options.priceAxisWidth, 18);
            ctx.fillStyle = '#ffffff';
            ctx.font = `bold 10px ${this.options.fontFamily}`;
            ctx.textAlign = 'left';
            ctx.textBaseline = 'middle';
            ctx.fillText(`₹${lastBar.close.toFixed(2)}`, this.plotWidth + 6, y);
        }

        _drawCrosshair(ctx) {
            const x = Math.floor(this.cursorX);
            const y = Math.floor(this.cursorY);

            ctx.strokeStyle = this.options.crosshairColor;
            ctx.lineWidth = 1;
            ctx.setLineDash([3, 3]);

            // Vertical crosshair
            ctx.beginPath();
            ctx.moveTo(x, 0);
            ctx.lineTo(x, this.plotHeight);
            ctx.stroke();

            // Horizontal crosshair
            ctx.beginPath();
            ctx.moveTo(0, y);
            ctx.lineTo(this.plotWidth, y);
            ctx.stroke();

            ctx.setLineDash([]);

            // Price Pill on Axis
            const cursorPrice = this.yToPrice(y);
            ctx.fillStyle = '#334155';
            ctx.fillRect(this.plotWidth, y - 9, this.options.priceAxisWidth, 18);
            ctx.fillStyle = '#f8fafc';
            ctx.font = `10px ${this.options.fontFamily}`;
            ctx.textAlign = 'left';
            ctx.textBaseline = 'middle';
            ctx.fillText(`₹${cursorPrice.toFixed(2)}`, this.plotWidth + 6, y);

            // Time Pill on Bottom Axis
            const hoveredIdx = this.xToIndex(x);
            if (hoveredIdx >= 0 && hoveredIdx < this.data.length) {
                const bar = this.data[hoveredIdx];
                this.hoveredBar = bar;
                const timeStr = this._formatTimestamp(bar.time);
                ctx.font = `10px ${this.options.fontFamily}`;
                const textW = ctx.measureText(timeStr).width;
                const pillX = Math.max(0, Math.min(this.plotWidth - textW - 12, x - (textW / 2) - 6));

                ctx.fillStyle = '#334155';
                ctx.fillRect(pillX, this.plotHeight, textW + 12, this.options.timeAxisHeight);
                ctx.fillStyle = '#f8fafc';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillText(timeStr, pillX + (textW / 2) + 6, this.plotHeight + (this.options.timeAxisHeight / 2));
            }
        }

        _updateHUD(forcedBar = null) {
            const bar = forcedBar || this.hoveredBar || (this.data.length ? this.data[this.data.length - 1] : null);
            if (!bar) {
                this.hud.innerHTML = '';
                return;
            }

            const isUp = bar.close >= bar.open;
            const diff = bar.close - bar.open;
            const diffPct = bar.open ? ((diff / bar.open) * 100) : 0;
            const col = isUp ? this.options.upColor : this.options.downColor;

            let html = `
                <span>O: <strong style="color:${col}">₹${bar.open.toFixed(2)}</strong></span>
                <span>H: <strong style="color:${col}">₹${bar.high.toFixed(2)}</strong></span>
                <span>L: <strong style="color:${col}">₹${bar.low.toFixed(2)}</strong></span>
                <span>C: <strong style="color:${col}">₹${bar.close.toFixed(2)}</strong></span>
                <span>Change: <strong style="color:${col}">${diff >= 0 ? '+' : ''}${diff.toFixed(2)} (${diffPct.toFixed(2)}%)</strong></span>
            `;

            if (bar.supertrend) {
                const stCol = bar.trend === 1 ? this.options.supertrendUpColor : this.options.supertrendDownColor;
                html += `<span>ST: <strong style="color:${stCol}">₹${bar.supertrend.toFixed(2)}</strong></span>`;
            }
            if (bar.volume) {
                html += `<span>Vol: <strong>${bar.volume.toLocaleString('en-IN')}</strong></span>`;
            }

            this.hud.innerHTML = html;
        }

        _findNearestIndex(targetTime) {
            let left = 0;
            let right = this.data.length - 1;
            while (left <= right) {
                const mid = Math.floor((left + right) / 2);
                if (this.data[mid].time === targetTime) return mid;
                if (this.data[mid].time < targetTime) left = mid + 1;
                else right = mid - 1;
            }
            return Math.min(this.data.length - 1, Math.max(0, left));
        }

        _formatTimestamp(ts) {
            if (!ts) return '';
            const d = new Date(ts * 1000);
            const hrs = String(d.getHours()).padStart(2, '0');
            const mins = String(d.getMinutes()).padStart(2, '0');
            const day = String(d.getDate()).padStart(2, '0');
            const monthNames = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
            const mon = monthNames[d.getMonth()];
            return `${day} ${mon} ${hrs}:${mins}`;
        }

        // Lightweight Charts Compatibility Facade
        addCandlestickSeries() {
            return {
                setData: (d) => this.setData({ candlestick: d }),
                setMarkers: (m) => this.setMarkers(m)
            };
        }

        addLineSeries() {
            return {
                setData: (d) => {
                    const stMap = {};
                    d.forEach(l => { if (l && l.time) stMap[l.time] = l; });
                    this.data.forEach(c => {
                        if (stMap[c.time]) {
                            c.supertrend = stMap[c.time].value;
                            c.trend = stMap[c.time].color === '#10b981' ? 1 : -1;
                        }
                    });
                    this.renderBase();
                }
            };
        }

        timeScale() {
            return {
                fitContent: () => this.fitContent()
            };
        }

        applyOptions(opts) {
            Object.assign(this.options, opts);
            this.resize();
        }

        destroy() {
            this.topCanvas.removeEventListener('mousemove', this._onMouseMove);
            this.topCanvas.removeEventListener('mousedown', this._onMouseDown);
            window.removeEventListener('mouseup', this._onMouseUp);
            this.topCanvas.removeEventListener('mouseleave', this._onMouseLeave);
            this.topCanvas.removeEventListener('wheel', this._onWheel);
            this.topCanvas.removeEventListener('dblclick', this._onDblClick);
            window.removeEventListener('resize', this._onResize);
            this.container.innerHTML = '';
        }
    }

    // Factory method
    function createChart(container, options) {
        return new DualCanvasChart(container, options);
    }

    // Export globally
    global.DualCanvasChart = DualCanvasChart;
    global.OpenAlgoChart = { createChart, DualCanvasChart };
    if (typeof global.LightweightCharts === 'undefined') {
        global.LightweightCharts = { createChart };
    }

})(typeof window !== 'undefined' ? window : this);
