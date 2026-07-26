(() => {
  "use strict";

  const report = window.__BACKTEST_REPORT__ || {};
  const equity = Array.isArray(report.equity) ? report.equity : [];
  const details = report.details && typeof report.details === "object" ? report.details : {};
  const embeddedMinuteDetails = report.minute_details && typeof report.minute_details === "object"
    ? report.minute_details
    : {};
  const reportKind = String(report.report_kind || "");
  const returnBasedReport = reportKind === "kdj_signal" || reportKind === "intraday_breakout_ytd";
  const configuredEquitySeries = Array.isArray(report.equity_series) ? report.equity_series : [];
  const equityHoverFields = Array.isArray(report.equity_hover_fields)
    ? report.equity_hover_fields
    : [];
  const plotConfig = {
    responsive: true,
    displaylogo: false,
    modeBarButtonsToRemove: ["lasso2d", "select2d"],
  };
  const baseLayout = {
    paper_bgcolor: "#10161a",
    plot_bgcolor: "#10161a",
    font: {color: "#cbd4d8", family: "Inter, Segoe UI, sans-serif"},
    margin: {t: 30, r: 24, b: 54, l: 68},
    hovermode: "x unified",
    xaxis: {gridcolor: "#263138", zerolinecolor: "#263138"},
    yaxis: {gridcolor: "#263138", zerolinecolor: "#263138"},
  };

  const modal = document.getElementById("detail-modal");
  const modalTitle = document.getElementById("detail-modal-title");
  const modalStatus = document.getElementById("modal-status");
  const directLink = document.getElementById("detail-direct-link");
  const previousRound = document.getElementById("round-prev");
  const nextRound = document.getElementById("round-next");
  const dailyViewButton = document.getElementById("detail-daily-view");
  const dayTabs = document.getElementById("detail-day-tabs");
  const minuteStatus = document.getElementById("detail-minute-status");
  const detailViewTitle = document.getElementById("detail-view-title");
  const minuteLoadPromises = new Map();
  let activeSymbol = "";
  let activeWindow = 0;
  let activeMinuteUrl = "";
  let activeViewDay = "";
  let lastFocused = null;

  function hasPlotly() {
    return window.Plotly && typeof window.Plotly.newPlot === "function";
  }

  function resetPlotlyChart(chart) {
    if (window.Plotly && typeof window.Plotly.purge === "function") {
      window.Plotly.purge(chart);
    }
    chart.classList.remove("chart-fallback");
    chart.innerHTML = "";
  }

  function showChartFallback(elementId) {
    const chart = document.getElementById(elementId);
    if (!chart) return;
    chart.classList.add("chart-fallback");
    chart.textContent = "Plotly 图表运行库未加载。请联网后刷新报告；表格、指标和交易数据仍可阅读。";
  }

  function pctText(value) {
    const number = Number(value);
    return Number.isFinite(number) ? `${number >= 0 ? "+" : ""}${(number * 100).toFixed(2)}%` : "—";
  }

  function moneyText(value) {
    const number = Number(value);
    return Number.isFinite(number)
      ? new Intl.NumberFormat("en-US", {style: "currency", currency: "USD"}).format(number)
      : "—";
  }

  function priceText(value) {
    const number = Number(value);
    return Number.isFinite(number) ? `$${number.toFixed(4)}` : "—";
  }

  function escapeText(value) {
    const node = document.createElement("span");
    node.textContent = value == null ? "" : String(value);
    return node.innerHTML;
  }

  function renderEquityChart() {
    if (!document.getElementById("equity-chart")) return;
    if (!hasPlotly()) {
      showChartFallback("equity-chart");
      return;
    }
    const configuredTraces = configuredEquitySeries.map((series, index) => {
      const hoverTemplate = [
        "%{x|%Y-%m-%d}",
        `${String(series.name || series.key || "Series")}: %{y:${String(series.value_format || ".2f")}}${String(series.value_suffix || "")}`,
        ...equityHoverFields.map((field, fieldIndex) =>
          `${String(field.label || field.key || "")}: %{customdata[${fieldIndex}]}`
        ),
        "<extra></extra>",
      ].join("<br>");
      return {
        x: equity.map(row => row.timestamp),
        y: equity.map(row => row[series.key]),
        customdata: equity.map(row =>
          equityHoverFields.map(field => row[field.key] == null ? "—" : row[field.key])
        ),
        mode: "lines",
        type: "scatter",
        name: String(series.name || series.key || `Series ${index + 1}`),
        line: {
          color: String(series.color || ["#ffb547", "#4dd7e5", "#ff6577"][index % 3]),
          width: Number(series.width || 2),
          dash: String(series.dash || "solid"),
        },
        hovertemplate: hoverTemplate,
      };
    });
    const defaultTraces = [{
        x: equity.map(row => row.timestamp),
        y: equity.map(row => row.equity),
        mode: "lines",
        type: "scatter",
        name: "Equity",
        line: {color: "#ffb547", width: 2.4},
        fill: "tozeroy",
        fillcolor: "rgba(255,181,71,.08)",
      }, {
        x: equity.map(row => row.timestamp),
        y: equity.map(row => row.cash),
        mode: "lines",
        type: "scatter",
        name: "Cash",
        line: {color: "#4dd7e5", width: 1.4, dash: "dot"},
      }];
    window.Plotly.newPlot("equity-chart", configuredTraces.length ? configuredTraces : defaultTraces, {
      ...baseLayout,
      yaxis: {...baseLayout.yaxis, title: String(report.equity_yaxis_title || "USD")},
      xaxis: {...baseLayout.xaxis, title: String(report.equity_xaxis_title || "Time")},
    }, plotConfig);
  }

  function renderEventRail(windowData) {
    const events = [];
    if (windowData.signal_day) events.push({kind: "signal", label: "Signal", value: windowData.signal_day});
    if (windowData.buy_day) events.push({kind: "buy", label: "Buy", value: windowData.buy_day});
    (windowData.sell_days || []).forEach(day => events.push({kind: "sell", label: "Sell", value: day}));
    document.getElementById("detail-event-rail").innerHTML = events.map(event =>
      `<div class="event ${event.kind}"><span>${event.label}</span><strong>${escapeText(event.value)}</strong></div>`
    ).join("") || "<p class='note'>无事件日期。</p>";
  }

  function makeMaTrace(rows, key, name, color, dash) {
    const values = rows.filter(row => Number.isFinite(Number(row[key])));
    return {
      x: values.map(row => row.timestamp),
      y: values.map(row => row[key]),
      type: "scatter",
      mode: "lines",
      name,
      line: {color, width: name === "MA5" ? 2.4 : 1.4, dash},
      hovertemplate: `${name}: %{y:.4f}<extra></extra>`,
    };
  }

  function updateRoundControls(windowCount) {
    previousRound.disabled = activeWindow <= 0;
    nextRound.disabled = activeWindow >= windowCount - 1;
  }

  function updateDeepLink() {
    if (!activeSymbol) return;
    const params = new URLSearchParams();
    params.set("symbol", activeSymbol);
    params.set("round", String(activeWindow + 1));
    window.history.replaceState(null, "", `#${params.toString()}`);
    directLink.href = window.location.href;
  }

  function dayFromValue(value) {
    const textValue = String(value || "");
    const direct = textValue.match(/^\d{4}-\d{2}-\d{2}/);
    if (direct) return direct[0];
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? "" : parsed.toISOString().slice(0, 10);
  }

  function ohlcText(location) {
    if (!location || !location.matched) return "OHLC —";
    return `O ${Number(location.open).toFixed(4)} / H ${Number(location.high).toFixed(4)} / L ${Number(location.low).toFixed(4)} / C ${Number(location.close).toFixed(4)}`;
  }

  function renderTradeLocations(windowData, minuteDay = "") {
    const trades = (Array.isArray(windowData.trades) ? windowData.trades : [])
      .filter(row => !minuteDay || dayFromValue(row.time) === minuteDay);
    const target = document.getElementById("detail-trade-locations");
    if (!trades.length) {
      target.innerHTML = minuteDay
        ? `<div class="trade-location"><div><strong>${escapeText(minuteDay)} 当天没有成交</strong><small>仍可查看当天分钟 K。</small></div></div>`
        : "";
      return;
    }
    target.innerHTML = trades.map(row => {
      const location = minuteDay ? row.minute_kline_location : row.kline_location;
      const warning = !location?.matched || location.status === "outside";
      const scope = minuteDay ? "分钟 K" : "日 K";
      const timeLabel = minuteDay ? row.time.replace("T", " ") : `${row.timestamp} · ${row.time.split("T")[1] || ""}`;
      const exact = minuteDay && location?.exact ? "精确匹配成交分钟" : location?.position || `缺少对应${scope}`;
      return `<div class="trade-location ${row.side === "SELL" ? "sell" : "buy"} ${warning ? "warning" : ""}">`
        + `<span class="trade-location-badge">${escapeText(row.side)}</span>`
        + `<div><strong>${escapeText(timeLabel)} · ${escapeText(priceText(row.price))} · ${escapeText(exact)}</strong>`
        + `<small>${escapeText(ohlcText(location))} · 数量 ${escapeText(row.quantity)} · ${escapeText(row.rule || "—")}</small></div></div>`;
    }).join("");
  }

  function makeTradeTrace(trades, side, minuteView) {
    const selected = trades.filter(row => row.side === side);
    const color = side === "BUY" ? "#4dd7e5" : "#ff6577";
    const locationKey = minuteView ? "minute_kline_location" : "kline_location";
    return {
      x: selected.map(row => minuteView ? row.time : row.timestamp),
      y: selected.map(row => row.price),
      mode: "markers",
      type: "scatter",
      name: side === "BUY" ? "Buy" : "Sell",
      marker: {
        color,
        size: 14,
        symbol: side === "BUY" ? "triangle-up" : "triangle-down",
        line: {color: "#071013", width: 1},
      },
      customdata: selected.map(row => {
        const location = row[locationKey] || {};
        return [
          row.time,
          row.rule,
          location.position || "—",
          ohlcText(location),
          returnBasedReport ? row.return_pct : row.realized_pnl,
        ];
      }),
      hovertemplate: `${side} %{y:.4f}<br>%{customdata[0]}<br>%{customdata[2]}<br>%{customdata[3]}<br>%{customdata[1]}`
        + (side === "SELL"
          ? returnBasedReport
            ? "<br>收益 %{customdata[4]:.2f}%"
            : "<br>PnL $%{customdata[4]:.2f}"
          : "")
        + "<extra></extra>",
    };
  }

  function makeTradeAnnotations(trades, minuteView) {
    return trades.map((row, index) => {
      const buy = row.side === "BUY";
      const stagger = (index % 3) * 12;
      return {
        x: minuteView ? row.time : row.timestamp,
        y: row.price,
        xref: "x",
        yref: "y",
        text: `${row.side}<br>${priceText(row.price)}`,
        showarrow: true,
        arrowhead: 2,
        arrowsize: 1,
        arrowwidth: 1.4,
        arrowcolor: buy ? "#4dd7e5" : "#ff6577",
        ax: buy ? -44 - stagger : 44 + stagger,
        ay: buy ? 52 + stagger : -52 - stagger,
        bgcolor: "rgba(7,16,19,.92)",
        bordercolor: buy ? "#4dd7e5" : "#ff6577",
        borderpad: 4,
        font: {color: buy ? "#4dd7e5" : "#ff6577", size: 10},
      };
    });
  }

  function windowDays(windowData) {
    const values = [
      windowData.signal_day,
      windowData.buy_day,
      ...(windowData.sell_days || []),
      ...(windowData.trades || []).map(row => dayFromValue(row.time)),
    ].filter(Boolean);
    return Array.from(new Set(values)).sort();
  }

  function setDayControlState(day = "") {
    activeViewDay = day;
    dailyViewButton.classList.toggle("active", !day);
    dailyViewButton.setAttribute("aria-pressed", String(!day));
    dayTabs.querySelectorAll("[data-minute-day]").forEach(button => {
      const selected = button.dataset.minuteDay === day;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
  }

  function renderDayControls(windowData) {
    const available = new Set(details[activeSymbol]?.minute_days || []);
    dayTabs.innerHTML = windowDays(windowData).map(day =>
      `<button class="day-tab" type="button" data-minute-day="${escapeText(day)}" `
      + `data-available="${available.has(day)}" aria-pressed="false">${escapeText(day)} · 1Min</button>`
    ).join("");
    dayTabs.querySelectorAll("[data-minute-day]").forEach(button => {
      button.addEventListener("click", () => showMinuteDay(button.dataset.minuteDay));
    });
    minuteStatus.textContent = available.size
      ? `本股票有 ${available.size} 个日期保存了分钟 K；也可直接点击图中的任意日 K。`
      : "本股票没有随本次回测保存的分钟 K；点击日期后会显示明确缺失状态。";
    setDayControlState("");
  }

  function loadMinuteDetail(symbol, url) {
    window.__BACKTEST_MINUTE_DETAILS__ = window.__BACKTEST_MINUTE_DETAILS__ || {};
    if (window.__BACKTEST_MINUTE_DETAILS__[symbol]) {
      return Promise.resolve(window.__BACKTEST_MINUTE_DETAILS__[symbol]);
    }
    if (embeddedMinuteDetails[symbol]) {
      window.__BACKTEST_MINUTE_DETAILS__[symbol] = embeddedMinuteDetails[symbol];
      return Promise.resolve(embeddedMinuteDetails[symbol]);
    }
    if (!url) return Promise.reject(new Error("没有分钟数据文件地址"));
    if (minuteLoadPromises.has(symbol)) return minuteLoadPromises.get(symbol);
    const promise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = url;
      script.async = true;
      script.onload = () => {
        const payload = window.__BACKTEST_MINUTE_DETAILS__?.[symbol];
        if (payload) resolve(payload);
        else reject(new Error("分钟数据文件未注册股票数据"));
      };
      script.onerror = () => reject(new Error("分钟数据文件加载失败"));
      document.head.appendChild(script);
    }).catch(error => {
      minuteLoadPromises.delete(symbol);
      throw error;
    });
    minuteLoadPromises.set(symbol, promise);
    return promise;
  }

  function showMinuteEmpty(day, message, windowData) {
    const chart = document.getElementById("detail-chart");
    resetPlotlyChart(chart);
    chart.innerHTML = `<div class="minute-empty"><div><strong>${escapeText(day)} 分钟 K 不可用</strong><br>${escapeText(message)}</div></div>`;
    detailViewTitle.textContent = `${activeSymbol} / ${day} / 1 分钟收盘价折线`;
    minuteStatus.textContent = `${message} 未回退到其他日期，也未把成交点吸附到相邻 K 线。`;
    renderTradeLocations(windowData, day);
  }

  function renderDailyChart(windowData) {
    const rows = Array.isArray(windowData.bars) ? windowData.bars : [];
    const trades = Array.isArray(windowData.trades) ? windowData.trades : [];
    const chart = document.getElementById("detail-chart");
    resetPlotlyChart(chart);
    setDayControlState("");
    detailViewTitle.textContent = `${activeSymbol} / 日 K 交易窗口`;
    renderTradeLocations(windowData);
    if (!hasPlotly()) {
      showChartFallback("detail-chart");
      return;
    }
    const compactChart = window.innerWidth < 640;
    const traces = [{
      x: rows.map(row => row.timestamp),
      open: rows.map(row => row.open),
      high: rows.map(row => row.high),
      low: rows.map(row => row.low),
      close: rows.map(row => row.close),
      type: "candlestick",
      name: `${activeSymbol} 日K`,
      increasing: {line: {color: "#ff6577"}, fillcolor: "rgba(255,101,119,.30)"},
      decreasing: {line: {color: "#50d890"}, fillcolor: "rgba(80,216,144,.26)"},
      hovertext: rows.map(row => `涨跌 ${pctText(row.daily_return_pct)}<br>VWAP ${row.vwap ?? "—"}<br>点击查看当天 1Min K`),
      hoverinfo: "x+open+high+low+close+text",
    },
    makeMaTrace(rows, "ma5", "MA5", "#ffb547", "solid"),
    makeMaTrace(rows, "ma10", "MA10", "#4dd7e5", "solid"),
    makeMaTrace(rows, "ma20", "MA20", "#b894ff", "dot"),
    {
      x: rows.map(row => row.timestamp),
      y: rows.map(row => row.volume),
      type: "bar",
      name: "Volume",
      yaxis: "y2",
      marker: {color: rows.map(row => row.close >= row.open ? "rgba(255,101,119,.36)" : "rgba(80,216,144,.32)")},
      hovertemplate: "Volume: %{y:,.0f}<extra></extra>",
    },
    makeTradeTrace(trades, "BUY", false),
    makeTradeTrace(trades, "SELL", false)];
    const signalShape = windowData.signal_day ? [{
      type: "line",
      x0: windowData.signal_day,
      x1: windowData.signal_day,
      yref: "paper",
      y0: 0,
      y1: 1,
      line: {color: "#ffb547", width: 1.2, dash: "dot"},
    }] : [];
    const annotations = makeTradeAnnotations(trades, false);
    if (windowData.signal_day) {
      annotations.push({
        x: windowData.signal_day,
        y: 1,
        yref: "paper",
        text: "SIGNAL",
        showarrow: false,
        xanchor: "left",
        yanchor: "bottom",
        font: {color: "#ffb547", size: 10},
      });
    }
    return Promise.resolve().then(() => window.Plotly.newPlot(chart, traces, {
      ...baseLayout,
      margin: compactChart ? {t: 84, r: 24, b: 58, l: 50} : {t: 46, r: 78, b: 62, l: 68},
      xaxis: {...baseLayout.xaxis, type: "category", rangeslider: {visible: false}, tickangle: -25},
      yaxis: {...baseLayout.yaxis, title: "Price", domain: [.27, 1]},
      yaxis2: {title: "Volume", domain: [0, .17], gridcolor: "#263138", fixedrange: true},
      legend: {orientation: "h", x: 0, y: 1.12},
      shapes: signalShape,
      annotations,
    }, plotConfig)).then(() => {
      if (typeof chart.removeAllListeners === "function") chart.removeAllListeners("plotly_click");
      if (typeof chart.on === "function") {
        chart.on("plotly_click", event => {
          const day = dayFromValue(event?.points?.[0]?.x);
          if (day) showMinuteDay(day);
        });
      }
    }).catch(error => {
      console.error("日 K 图表渲染失败", error);
      if (activeViewDay) return;
      resetPlotlyChart(chart);
      chart.classList.add("chart-fallback");
      chart.textContent = `日 K 图表渲染失败：${error?.message || "未知错误"}。请刷新报告后重试。`;
    });
  }

  function renderMinuteChart(day, payload, windowData) {
    const compactRows = payload?.days?.[day];
    if (!Array.isArray(compactRows) || compactRows.length === 0) {
      showMinuteEmpty(day, "本次回测缓存中没有该日的分钟 K。", windowData);
      return;
    }
    const rows = compactRows.map(row => ({
      timestamp: row[0],
      open: row[1],
      high: row[2],
      low: row[3],
      close: row[4],
    })).sort((left, right) => String(left.timestamp).localeCompare(String(right.timestamp)));
    const trades = (windowData.trades || []).filter(row => dayFromValue(row.time) === day);
    const chart = document.getElementById("detail-chart");
    resetPlotlyChart(chart);
    detailViewTitle.textContent = `${activeSymbol} / ${day} / 1 分钟收盘价折线`;
    minuteStatus.textContent = `${day} · ${rows.length} 个真实分钟点，按时间连续连接。买卖箭头尖端使用完整成交时间和成交价。`;
    renderTradeLocations(windowData, day);
    if (!hasPlotly()) {
      showMinuteEmpty(day, "Plotly 图表运行库未加载。请联网后刷新报告。", windowData);
      return Promise.resolve();
    }
    const traces = [{
      x: rows.map(row => row.timestamp),
      y: rows.map(row => row.close),
      customdata: rows.map(row => [row.open, row.high, row.low]),
      type: "scatter",
      mode: "lines",
      name: `${activeSymbol} 1Min Close`,
      connectgaps: true,
      line: {color: "#55dbe8", width: 2.4, shape: "linear"},
      hovertemplate: "%{x|%H:%M}<br>Close %{y:.4f}<br>Open %{customdata[0]:.4f}<br>High %{customdata[1]:.4f}<br>Low %{customdata[2]:.4f}<extra></extra>",
    },
    makeTradeTrace(trades, "BUY", true),
    makeTradeTrace(trades, "SELL", true)];
    return Promise.resolve().then(() => window.Plotly.newPlot(chart, traces, {
      ...baseLayout,
      margin: window.innerWidth < 640 ? {t: 82, r: 22, b: 60, l: 50} : {t: 46, r: 78, b: 62, l: 68},
      xaxis: {
        ...baseLayout.xaxis,
        type: "date",
        title: `${day} / America/New_York`,
        tickformat: "%H:%M",
        rangeslider: {visible: false},
      },
      yaxis: {...baseLayout.yaxis, title: "Price"},
      legend: {orientation: "h", x: 0, y: 1.12},
      annotations: makeTradeAnnotations(trades, true),
    }, plotConfig)).catch(error => {
      console.error("分钟图表渲染失败", error);
      throw new Error(`分钟图表渲染失败：${error?.message || "未知错误"}`);
    });
  }

  function showMinuteDay(day) {
    const windowData = details[activeSymbol]?.windows?.[activeWindow];
    if (!windowData || !day) return;
    const requestedSymbol = activeSymbol;
    const requestedWindow = activeWindow;
    setDayControlState(day);
    detailViewTitle.textContent = `${activeSymbol} / ${day} / 正在加载…`;
    minuteStatus.textContent = `${day} 分钟折线正在加载…`;
    loadMinuteDetail(activeSymbol, activeMinuteUrl).then(payload => {
      if (activeSymbol !== requestedSymbol || activeWindow !== requestedWindow || activeViewDay !== day) return;
      return renderMinuteChart(day, payload, windowData);
    }).catch(error => {
      if (activeSymbol !== requestedSymbol || activeWindow !== requestedWindow || activeViewDay !== day) return;
      showMinuteEmpty(day, error.message || "分钟数据加载失败。", windowData);
    });
  }

  function renderDetailWindow(index, options = {}) {
    const detail = details[activeSymbol];
    const windows = detail && Array.isArray(detail.windows) ? detail.windows : [];
    const windowData = windows[index];
    if (!windowData) return;
    activeWindow = index;
    document.querySelectorAll(".window-tab").forEach((tab, tabIndex) => {
      const selected = tabIndex === index;
      tab.classList.toggle("active", selected);
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      if (selected && options.focusTab) tab.focus();
    });
    updateRoundControls(windows.length);
    renderEventRail(windowData);
    renderDayControls(windowData);
    document.getElementById("detail-note").textContent = windowData.title;
    const rows = Array.isArray(windowData.bars) ? windowData.bars : [];
    const trades = Array.isArray(windowData.trades) ? windowData.trades : [];
    const buys = trades.filter(row => row.side === "BUY");
    const firstBuy = buys[0];
    const summaryRows = reportKind === "intraday_breakout_ytd"
      ? [
        ["交易日", windowData.buy_day || "—"],
        ["买点", String(windowData.entry_time || "—").replace("T", " ")],
        ["卖点", String(windowData.exit_time || "—").replace("T", " ")],
        ["买入价", firstBuy ? priceText(firstBuy.price) : "—"],
        ["全天最高涨幅", `${Number(windowData.session_high_gain_pct) >= 0 ? "+" : ""}${Number(windowData.session_high_gain_pct || 0).toFixed(2)}%`],
        ["全天最低涨幅", `${Number(windowData.session_low_gain_pct) >= 0 ? "+" : ""}${Number(windowData.session_low_gain_pct || 0).toFixed(2)}%`],
        ["本轮毛收益率", `${Number(windowData.return_pct) >= 0 ? "+" : ""}${Number(windowData.return_pct || 0).toFixed(2)}%`],
      ]
      : reportKind === "kdj_signal"
      ? [
        ["信号日", windowData.signal_day || "—"],
        ["买点", String(windowData.entry_time || "—").replace("T", " ")],
        ["卖点", String(windowData.exit_time || "—").replace("T", " ")],
        ["买入价", firstBuy ? priceText(firstBuy.price) : "—"],
        ["本轮收益率", `${Number(windowData.return_pct) >= 0 ? "+" : ""}${Number(windowData.return_pct || 0).toFixed(2)}%`],
        ["股票逐笔复合", `${Number(detail.symbol_compounded_return_pct) >= 0 ? "+" : ""}${Number(detail.symbol_compounded_return_pct || 0).toFixed(2)}%`],
      ]
      : [
        ["信号日", windowData.signal_day || "—"],
        ["买入日", windowData.buy_day || "—"],
        ["买入价", firstBuy ? priceText(firstBuy.price) : "—"],
        ["本轮已实现收益", moneyText(windowData.realized_pnl)],
        ["股票累计收益", moneyText(detail.symbol_realized_pnl)],
        ["窗口日 K", String(rows.length)],
      ];
    document.getElementById("detail-summary").innerHTML = summaryRows.map(([label, value]) =>
      `<div class="detail-stat">${escapeText(label)}<strong>${escapeText(value)}</strong></div>`
    ).join("");
    renderDailyChart(windowData);
    updateDeepLink();
    modalStatus.textContent = `${activeSymbol}，第 ${activeWindow + 1} 轮，共 ${windows.length} 轮`;
  }

  function closeDetailModal(options = {}) {
    if (!modal) return;
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
    activeSymbol = "";
    activeMinuteUrl = "";
    activeViewDay = "";
    if (!options.keepHash) window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
    if (lastFocused && !options.skipFocus) lastFocused.focus();
  }

  function openDetail(button, requestedWindow = 0, options = {}) {
    activeSymbol = button.dataset.symbol;
    activeMinuteUrl = button.dataset.minuteUrl || "";
    const detail = details[activeSymbol];
    if (!detail || !Array.isArray(detail.windows) || detail.windows.length === 0) return;
    lastFocused = options.fromHash ? null : button;
    modalTitle.textContent = `${activeSymbol} / K 线证据`;
    directLink.href = button.dataset.detailUrl;
    const tabs = document.getElementById("detail-window-tabs");
    tabs.innerHTML = detail.windows.map((_windowData, index) =>
      `<button class="window-tab" role="tab" type="button" data-window-index="${index}" aria-selected="false">ROUND ${String(index + 1).padStart(2, "0")}</button>`
    ).join("");
    tabs.querySelectorAll("[data-window-index]").forEach(tab => {
      tab.addEventListener("click", () => renderDetailWindow(Number(tab.dataset.windowIndex)));
    });
    modal.classList.add("open");
    modal.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
    const safeIndex = Math.max(0, Math.min(Number(requestedWindow) || 0, detail.windows.length - 1));
    renderDetailWindow(safeIndex);
    if (!options.fromHash) document.getElementById("detail-modal-close").focus();
  }

  function copyDeepLink() {
    updateDeepLink();
    const value = window.location.href;
    const done = () => {
      const button = document.getElementById("detail-copy-link");
      button.textContent = "已复制";
      modalStatus.textContent = "当前股票与交易轮次链接已复制";
      window.setTimeout(() => { button.textContent = "复制链接"; }, 1500);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(value).then(done).catch(() => legacyCopy(value, done));
    } else {
      legacyCopy(value, done);
    }
  }

  function legacyCopy(value, done) {
    const textarea = document.createElement("textarea");
    textarea.value = value;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand("copy");
    textarea.remove();
    done();
  }

  function shiftRound(delta) {
    if (!activeSymbol) return;
    const windows = details[activeSymbol]?.windows || [];
    const nextIndex = Math.max(0, Math.min(activeWindow + delta, windows.length - 1));
    if (nextIndex !== activeWindow) renderDetailWindow(nextIndex, {focusTab: true});
  }

  function bindModal() {
    if (!modal) return;
    document.querySelectorAll("[data-detail-url]").forEach(button => {
      button.addEventListener("click", () => {
        openDetail(button, Number(button.dataset.windowIndex || 0));
      });
    });
    document.getElementById("detail-modal-back").addEventListener("click", closeDetailModal);
    document.getElementById("detail-modal-close").addEventListener("click", closeDetailModal);
    document.getElementById("detail-copy-link").addEventListener("click", copyDeepLink);
    dailyViewButton.addEventListener("click", () => {
      const windowData = details[activeSymbol]?.windows?.[activeWindow];
      if (windowData) renderDailyChart(windowData);
    });
    previousRound.addEventListener("click", () => shiftRound(-1));
    nextRound.addEventListener("click", () => shiftRound(1));
    modal.addEventListener("click", event => {
      if (event.target === modal) closeDetailModal();
    });
    modal.addEventListener("keydown", event => {
      if (event.key !== "Tab") return;
      const focusable = Array.from(modal.querySelectorAll(
        "button:not([disabled]), a[href], input:not([disabled]), [tabindex]:not([tabindex='-1'])"
      )).filter(element => element.offsetParent !== null);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });
    window.addEventListener("keydown", event => {
      if (!modal.classList.contains("open")) return;
      if (event.key === "Escape") closeDetailModal();
      if (event.key === "ArrowLeft") shiftRound(-1);
      if (event.key === "ArrowRight") shiftRound(1);
    });
  }

  function bindTradeSort() {
    const realizedPnlSort = document.querySelector("[data-sort-realized-pnl]");
    if (!realizedPnlSort) return;
    realizedPnlSort.addEventListener("click", () => {
      const table = document.getElementById("trades-table");
      const tbody = table?.querySelector("tbody");
      if (!tbody) return;
      const nextDirection = realizedPnlSort.dataset.direction === "desc" ? "asc" : "desc";
      const rows = Array.from(tbody.querySelectorAll("tr"));
      rows.sort((left, right) => {
        const leftValue = Number(left.dataset.realizedPnl || 0);
        const rightValue = Number(right.dataset.realizedPnl || 0);
        return nextDirection === "desc" ? rightValue - leftValue : leftValue - rightValue;
      });
      rows.forEach(row => tbody.appendChild(row));
      realizedPnlSort.dataset.direction = nextDirection;
      realizedPnlSort.textContent = nextDirection === "desc" ? "已实现收益 ↓" : "已实现收益 ↑";
      realizedPnlSort.setAttribute(
        "aria-label",
        nextDirection === "desc" ? "已按已实现收益从高到低排序" : "已按已实现收益从低到高排序"
      );
    });
  }

  function bindSymbolFilters() {
    const symbolSearch = document.getElementById("symbol-search");
    if (!symbolSearch) return;
    const symbolTableBody = document.querySelector("#symbol-detail-table tbody");
    let symbolRows = Array.from(document.querySelectorAll("#symbol-detail-table tbody tr"));
    const symbolCount = document.getElementById("symbol-count");
    const filterButtons = Array.from(document.querySelectorAll("[data-symbol-filter]"));
    const symbolTimeSort = document.querySelector("[data-sort-symbol-time]");
    let activeFilter = "all";

    function updateVisibleRanks() {
      let visibleRank = 0;
      symbolRows.forEach(row => {
        if (!row.hidden) visibleRank += 1;
        const rank = row.querySelector("[data-row-rank]");
        if (rank && !row.hidden) rank.textContent = String(visibleRank).padStart(2, "0");
        row.classList.toggle("is-latest", !row.hidden && visibleRank === 1);
      });
    }

    function filterSymbols() {
      const query = symbolSearch.value.trim().toUpperCase();
      let visible = 0;
      symbolRows.forEach(row => {
        const pnl = Number(row.dataset.realizedPnl || 0);
        const rounds = Number(row.dataset.rounds || 0);
        const matchesText = !query || row.dataset.symbol.includes(query);
        const matchesFilter = activeFilter === "all"
          || (activeFilter === "profit" && pnl > 0)
          || (activeFilter === "loss" && pnl < 0)
          || (activeFilter === "multi" && rounds > 1);
        const matches = matchesText && matchesFilter;
        row.hidden = !matches;
        if (matches) visible += 1;
      });
      updateVisibleRanks();
      const direction = symbolTimeSort?.dataset.direction === "asc" ? "最早优先" : "最新优先";
      symbolCount.textContent = `${visible} / ${symbolRows.length} 笔交易 · ${direction}`;
    }

    function sortSymbolRows(direction) {
      if (!symbolTableBody) return;
      const factor = direction === "asc" ? 1 : -1;
      symbolRows.sort((left, right) => {
        const leftTime = Date.parse(left.dataset.latestTime || "") || 0;
        const rightTime = Date.parse(right.dataset.latestTime || "") || 0;
        if (leftTime !== rightTime) return (leftTime - rightTime) * factor;
        return String(left.dataset.symbol || "").localeCompare(String(right.dataset.symbol || ""));
      });
      symbolRows.forEach(row => symbolTableBody.appendChild(row));
      if (symbolTimeSort) {
        symbolTimeSort.dataset.direction = direction;
        symbolTimeSort.textContent = direction === "desc" ? "最新优先 ↓" : "最早优先 ↑";
        symbolTimeSort.setAttribute(
          "aria-label",
          direction === "desc"
            ? "当前按最近交易时间从新到旧排序"
            : "当前按最近交易时间从旧到新排序"
        );
      }
      filterSymbols();
    }

    symbolSearch.addEventListener("input", filterSymbols);
    filterButtons.forEach(button => {
      button.addEventListener("click", () => {
        activeFilter = button.dataset.symbolFilter;
        filterButtons.forEach(candidate => {
          const selected = candidate === button;
          candidate.classList.toggle("active", selected);
          candidate.setAttribute("aria-pressed", String(selected));
        });
        filterSymbols();
      });
    });
    symbolTimeSort?.addEventListener("click", () => {
      const nextDirection = symbolTimeSort.dataset.direction === "desc" ? "asc" : "desc";
      sortSymbolRows(nextDirection);
    });
    sortSymbolRows("desc");
  }

  function bindSectionNavigation() {
    const links = Array.from(document.querySelectorAll(".report-nav a"));
    const sections = Array.from(document.querySelectorAll("[data-report-section]"));
    if (!("IntersectionObserver" in window) || !links.length) return;
    const byId = new Map(links.map(link => [link.getAttribute("href").slice(1), link]));
    const observer = new IntersectionObserver(entries => {
      const visible = entries
        .filter(entry => entry.isIntersecting)
        .sort((left, right) => right.intersectionRatio - left.intersectionRatio)[0];
      if (!visible) return;
      links.forEach(link => link.classList.toggle("active", link === byId.get(visible.target.id)));
    }, {rootMargin: "-20% 0px -65% 0px", threshold: [0, .2, .6]});
    sections.forEach(section => observer.observe(section));
  }

  function openFromHash() {
    const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    const symbol = (params.get("symbol") || "").toUpperCase();
    const round = Math.max(0, Number(params.get("round") || 1) - 1);
    if (!symbol) return;
    const button = Array.from(document.querySelectorAll("[data-detail-url]"))
      .find(candidate => candidate.dataset.symbol === symbol);
    if (button) openDetail(button, round, {fromHash: true});
  }

  document.getElementById("print-report")?.addEventListener("click", () => window.print());
  renderEquityChart();
  bindModal();
  bindTradeSort();
  bindSymbolFilters();
  bindSectionNavigation();
  openFromHash();
})();
