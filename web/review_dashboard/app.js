"use strict";

const API = {
  dates: "/api/review/dates",
  review: "/api/review",
  evidence: "/api/review/evidence",
};

const state = {
  data: null,
  dates: [],
  requestToken: 0,
  brokerLoading: false,
  bucket: "",
  status: "",
  reason: "",
  search: "",
  sortKey: "priority",
  sortDirection: "asc",
  timelineDescending: false,
  selectedSymbol: null,
  drawerTab: "lifecycle",
  lastFocused: null,
};

const el = {};

document.addEventListener("DOMContentLoaded", init);

async function init() {
  cacheElements();
  bindEvents();
  renderMetricSkeletons();
  try {
    const response = await fetchJSON(API.dates);
    state.dates = Array.isArray(response.dates) ? response.dates : [];
  } catch (error) {
    showToast(`日期列表读取失败：${error.message}`, "warning");
  }
  const urlDate = new URL(location.href).searchParams.get("date");
  const initialDate = validDate(urlDate) ? urlDate : (state.dates[0] || "");
  await loadReview(initialDate);
}

function cacheElements() {
  const ids = [
    "menu-button", "section-nav", "previous-date", "next-date", "review-date", "review-status",
    "coverage-status", "broker-status", "refresh-button", "chart-link", "market-banner", "headline-title",
    "headline-detail", "generated-at", "conflict-banner", "conflict-title", "conflict-detail",
    "jump-to-attention", "metric-rail", "page-error", "page-error-message", "retry-button", "decision-workspace",
    "decision-count", "clear-filters", "quick-filters", "status-filter", "reason-filter", "symbol-search",
    "filter-summary-button", "active-filter-summary", "decision-table", "decision-table-body", "decision-empty",
    "filtered-total", "timeline-list", "timeline-order", "timeline-empty", "attention-panel", "attention-count",
    "attention-list", "attention-empty", "funnel-content", "lifecycle-content", "reasons-content", "orders-content",
    "orders-count", "phases-content", "health-content", "drawer-backdrop", "symbol-drawer", "drawer-previous",
    "drawer-next", "drawer-close", "drawer-title", "drawer-subtitle", "drawer-stats", "drawer-order-timeline",
    "drawer-checklist", "drawer-consistency", "drawer-strategy-context", "drawer-evidence-list",
    "drawer-evidence-context", "evidence-context-title", "evidence-context-output", "close-evidence-context",
    "show-symbol-orders", "copy-summary", "open-first-evidence", "copy-status", "toast-region", "app-live-region",
  ];
  ids.forEach((id) => { el[id] = document.getElementById(id); });
}

function bindEvents() {
  el["menu-button"].addEventListener("click", () => {
    const open = el["menu-button"].getAttribute("aria-expanded") === "true";
    el["menu-button"].setAttribute("aria-expanded", String(!open));
    el["section-nav"].hidden = open;
  });
  el["section-nav"].addEventListener("click", (event) => {
    if (event.target.closest("a")) {
      el["section-nav"].hidden = true;
      el["menu-button"].setAttribute("aria-expanded", "false");
    }
  });
  el["review-date"].addEventListener("change", () => loadReview(el["review-date"].value));
  el["previous-date"].addEventListener("click", () => navigateDate("previous"));
  el["next-date"].addEventListener("click", () => navigateDate("next"));
  el["refresh-button"].addEventListener("click", () => loadReview(state.data?.review_date || el["review-date"].value));
  el["retry-button"].addEventListener("click", () => loadReview(el["review-date"].value));
  el["jump-to-attention"].addEventListener("click", () => el["attention-panel"].scrollIntoView({ behavior: "smooth", block: "center" }));
  el["status-filter"].addEventListener("change", () => { state.status = el["status-filter"].value; renderDecisionTable(); });
  el["reason-filter"].addEventListener("change", () => { state.reason = el["reason-filter"].value; renderDecisionTable(); });
  el["symbol-search"].addEventListener("input", () => { state.search = el["symbol-search"].value.trim().toLowerCase(); renderDecisionTable(); });
  el["filter-summary-button"].addEventListener("click", () => {
    const open = !el["active-filter-summary"].hidden;
    el["active-filter-summary"].hidden = open;
    el["filter-summary-button"].setAttribute("aria-expanded", String(!open));
  });
  el["clear-filters"].addEventListener("click", clearFilters);
  el["decision-table"].querySelector("thead").addEventListener("click", handleSort);
  el["decision-table-body"].addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-symbol]");
    if (row) openDrawer(row.dataset.symbol, event.target.closest("button") || row);
  });
  el["decision-table-body"].addEventListener("keydown", (event) => {
    const row = event.target.closest("tr[data-symbol]");
    if (row && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      openDrawer(row.dataset.symbol, row);
    }
  });
  el["timeline-order"].addEventListener("click", () => { state.timelineDescending = !state.timelineDescending; renderTimeline(); });
  el["drawer-close"].addEventListener("click", closeDrawer);
  el["drawer-backdrop"].addEventListener("click", closeDrawer);
  el["drawer-previous"].addEventListener("click", () => moveDrawer(-1));
  el["drawer-next"].addEventListener("click", () => moveDrawer(1));
  document.querySelector(".drawer-tabs").addEventListener("click", (event) => {
    const tab = event.target.closest("[data-tab]");
    if (tab) setDrawerTab(tab.dataset.tab);
  });
  document.querySelector(".drawer-tabs").addEventListener("keydown", handleTabKeys);
  el["close-evidence-context"].addEventListener("click", () => { el["drawer-evidence-context"].hidden = true; });
  el["copy-summary"].addEventListener("click", copySelectedSummary);
  el["show-symbol-orders"].addEventListener("click", () => {
    const symbol = selectedSymbol();
    if (!symbol) return;
    state.search = symbol.ticker.toLowerCase();
    el["symbol-search"].value = symbol.ticker;
    closeDrawer();
    renderDecisionTable();
    el["decision-workspace"].scrollIntoView({ behavior: "smooth" });
  });
  el["open-first-evidence"].addEventListener("click", () => {
    const symbol = selectedSymbol();
    const evidence = symbol?.evidence?.[0] || symbol?.latest?.evidence;
    if (evidence) {
      setDrawerTab("evidence");
      loadEvidence(evidence.source_id, evidence.line);
    } else {
      showToast("该股票没有可展开的本地证据行。", "warning");
    }
  });
  document.addEventListener("keydown", handleGlobalKeys);
}

async function loadReview(dateValue) {
  const token = ++state.requestToken;
  setBusy(true);
  hideError();
  closeDrawer();
  try {
    const query = new URLSearchParams({ broker: "0" });
    if (validDate(dateValue)) query.set("date", dateValue);
    const local = await fetchJSON(`${API.review}?${query}`);
    if (token !== state.requestToken) return;
    state.data = local;
    syncDateState();
    renderAll();
    setBusy(false);
    state.brokerLoading = true;
    renderHeader();
    query.set("date", local.review_date);
    query.set("broker", "1");
    try {
      const reconciled = await fetchJSON(`${API.review}?${query}`);
      if (token !== state.requestToken) return;
      state.data = reconciled;
      state.brokerLoading = false;
      renderAll();
      announce("券商只读核对完成");
    } catch (brokerError) {
      if (token !== state.requestToken) return;
      state.brokerLoading = false;
      renderHeader();
      showToast(`券商核对未完成：${brokerError.message}`, "warning");
    }
  } catch (error) {
    if (token !== state.requestToken) return;
    setBusy(false);
    showError(error.message);
  }
}

function renderAll() {
  if (!state.data) return;
  renderHeader();
  renderHeadline();
  renderConflict();
  renderMetrics();
  populateFilters();
  renderQuickFilters();
  renderDecisionTable();
  renderTimeline();
  renderAttention();
  renderFunnel();
  renderLifecycle();
  renderReasons();
  renderOrders();
  renderPhases();
  renderHealth();
  if (state.selectedSymbol) renderDrawer();
}

function renderHeader() {
  const data = state.data;
  if (!data) return;
  el["review-date"].value = data.review_date || "";
  const complete = (data.summary?.rounds?.intraday || 0) > 0;
  el["review-status"].className = `status-indicator ${complete ? "success" : "warning"}`;
  el["review-status"].innerHTML = `<span class="status-dot" aria-hidden="true"></span><span>${complete ? "复盘已完成" : "数据不完整"}</span>`;
  const phaseRanges = data.phases || [];
  const first = phaseRanges.find((item) => item.start_at)?.start_at;
  const last = [...phaseRanges].reverse().find((item) => item.end_at)?.end_at;
  const coverage = first && last ? `${formatTime(first)}—${formatTime(last)} ET` : "覆盖时间不完整";
  el["coverage-status"].innerHTML = `${icon("clock")}<span>${coverage}</span>`;
  const brokerStatus = state.brokerLoading ? "loading" : data.quality?.broker_status;
  const brokerMap = {
    loading: ["warning", "正在只读核对…"],
    verified: ["success", `Alpaca ${String(data.broker?.mode || "").toUpperCase()} 已核对`],
    unavailable: ["warning", "Alpaca 核对不可用"],
    not_requested: ["neutral", "券商未核对"],
  };
  const [brokerClass, brokerLabel] = brokerMap[brokerStatus] || brokerMap.not_requested;
  el["broker-status"].className = `status-indicator ${brokerClass}`;
  el["broker-status"].innerHTML = `<span class="status-dot" aria-hidden="true"></span><span>${escapeHTML(brokerLabel)}</span>`;
  const chartUrl = data.chart_url;
  el["chart-link"].href = chartUrl || "#";
  el["chart-link"].setAttribute("aria-disabled", String(!chartUrl));
  el["chart-link"].tabIndex = chartUrl ? 0 : -1;
  el["generated-at"].textContent = `生成 ${formatDateTime(data.generated_at)}`;
  el["market-banner"].hidden = !data.market_day?.banner;
  el["market-banner"].textContent = data.market_day?.banner || "";
  updateDateButtons();
}

function renderHeadline() {
  const headline = state.data?.headline || {};
  el["headline-title"].textContent = headline.title || "当天复盘";
  el["headline-detail"].textContent = headline.detail || "暂无补充结论。";
}

function renderConflict() {
  const critical = (state.data?.attention || []).find((item) => item.severity === "critical");
  el["conflict-banner"].hidden = !critical;
  if (!critical) return;
  el["conflict-title"].textContent = `必须核对 · ${critical.title}`;
  el["conflict-detail"].textContent = critical.message;
}

function metricDefinitions() {
  const s = state.data?.summary || {};
  return [
    { label: "券商买入股票", value: num(s.broker_bought_symbols), subtitle: "至少有部分成交", icon: "bag", tone: "success", bucket: "broker_filled" },
    { label: "已全部卖出", value: num(s.broker_closed_symbols), subtitle: "按复盘日买卖成交量", icon: "check", tone: "success", bucket: "broker_closed" },
    { label: "券商订单", value: num(s.broker_order_count), subtitle: "含成交、取消与部分成交", icon: "order", tone: "info", bucket: "broker_activity" },
    { label: "未成交买入", value: num(s.broker_unfilled_buy_symbols), subtitle: "按股票去重", icon: "hourglass", tone: "warning", bucket: "buy_unfilled" },
    { label: "策略候选", value: num(s.watch_counts?.intraday), subtitle: "盘中 MA5 观察池", icon: "users", tone: "neutral", bucket: "strategy" },
    { label: "今日排除", value: num(s.excluded_count), subtitle: "当天不再考虑买入", icon: "ban", tone: "warning", bucket: "excluded" },
    { label: "当前持仓", value: num(s.current_positions), subtitle: "只读券商当前快照", icon: "wallet", tone: "neutral", bucket: "current_position" },
    { label: "当日成交净现金流", value: money(s.net_cash_flow), subtitle: "按成交额计算，未含费用", icon: "database", tone: cashTone(s.net_cash_flow), bucket: "cash_flow" },
  ];
}

function renderMetricSkeletons() {
  el["metric-rail"].innerHTML = Array.from({ length: 8 }, () => `<div class="metric-skeleton skeleton"></div>`).join("");
}

function renderMetrics() {
  el["metric-rail"].setAttribute("aria-busy", "false");
  el["metric-rail"].innerHTML = metricDefinitions().map((item) => `
    <button class="metric-button ${item.tone} ${state.bucket === item.bucket ? "active" : ""}" type="button" data-bucket="${item.bucket}" aria-pressed="${state.bucket === item.bucket}">
      <span class="metric-label">${escapeHTML(item.label)}</span>
      <span class="metric-value-row">${icon(item.icon)}<strong class="metric-value">${escapeHTML(item.value)}</strong></span>
      <span class="metric-subtitle">${escapeHTML(item.subtitle)}</span>
    </button>`).join("");
  el["metric-rail"].querySelectorAll("[data-bucket]").forEach((button) => button.addEventListener("click", () => setBucket(button.dataset.bucket)));
}

function renderQuickFilters() {
  const symbols = state.data?.symbols || [];
  const definitions = [
    ["", "全部", symbols.length],
    ["broker_filled", "券商已成交", symbols.filter((item) => ["broker_closed", "broker_bought"].includes(item.bucket)).length],
    ["buy_unfilled", "买入未成", symbols.filter((item) => item.bucket === "buy_unfilled").length],
    ["strategy", "策略未买", symbols.filter((item) => ["not_bought", "window_outside_closest", "excluded"].includes(item.bucket)).length],
    ["excluded", "今日排除", symbols.filter((item) => item.bucket === "excluded").length],
    ["data_conflict", "数据冲突", symbols.filter((item) => ["missing", "partial", "unmatched"].includes(item.local_ledger_match)).length],
  ];
  el["quick-filters"].innerHTML = definitions.map(([key, label, count]) => `
    <button type="button" data-bucket="${key}" class="${state.bucket === key ? "active" : ""}" aria-pressed="${state.bucket === key}">
      ${escapeHTML(label)} <strong>${count}</strong>
    </button>`).join("");
  el["quick-filters"].querySelectorAll("[data-bucket]").forEach((button) => button.addEventListener("click", () => setBucket(button.dataset.bucket)));
}

function populateFilters() {
  const symbols = state.data?.symbols || [];
  const statuses = [...new Set(symbols.map((item) => item.status_label).filter(Boolean))].sort(localeCompare);
  const reasons = [...new Map((state.data?.reason_distribution || []).map((item) => [item.code, item.label])).entries()];
  setSelectOptions(el["status-filter"], "全部状态", statuses.map((item) => [item, item]), state.status);
  setSelectOptions(el["reason-filter"], "全部原因", reasons, state.reason);
}

function renderDecisionTable() {
  if (!state.data) return;
  const items = filteredSymbols();
  el["decision-count"].textContent = `${items.length} / ${state.data.symbols?.length || 0} 只股票`;
  el["filtered-total"].textContent = `共 ${items.length} 只`;
  el["decision-empty"].hidden = items.length > 0;
  el["decision-table"].hidden = items.length === 0;
  el["clear-filters"].hidden = !filtersActive();
  renderFilterSummary();
  el["decision-table-body"].innerHTML = items.map((item) => decisionRow(item)).join("");
  updateSortHeaders();
}

function decisionRow(item) {
  const buy = item.buy_filled_qty > 0
    ? `买 ${formatQty(item.buy_filled_qty)} @ ${price(item.buy_avg_price)}`
    : (item.bucket === "buy_unfilled" ? `${item.orders?.filter((order) => order.side === "BUY").length || 0} 笔未成` : "—");
  const sell = item.sell_filled_qty > 0 ? `卖 ${formatQty(item.sell_filled_qty)} @ ${price(item.sell_avg_price)}` : "—";
  const value = valueCell(item);
  const sourceTags = (item.source_labels || []).map((source) => `<span class="source-tag">${icon(source.startsWith("Alpaca") ? "database" : "file")} ${escapeHTML(source)}</span>`).join("");
  const match = matchLabel(item.local_ledger_match);
  const selected = state.selectedSymbol === item.symbol;
  return `
    <tr data-symbol="${escapeAttr(item.symbol)}" tabindex="0" class="${selected ? "selected" : ""}" aria-label="打开 ${escapeAttr(item.ticker)} 详情">
      <td><span class="symbol-cell">${escapeHTML(item.ticker)}</span></td>
      <td><span class="source-stack">${sourceTags || "—"}</span></td>
      <td><span class="status-cell severity-${severityClass(item.severity)}"><i class="status-shape" aria-hidden="true"></i><span class="status-label">${escapeHTML(item.status_label || item.bucket)}</span></span></td>
      <td class="number-cell">${escapeHTML(buy)}</td>
      <td class="number-cell">${escapeHTML(sell)}</td>
      <td class="${value.className}">${escapeHTML(value.text)}</td>
      <td>${escapeHTML(symbolTime(item))}</td>
      <td><span class="match-cell ${match.className}">${icon(match.icon)} ${escapeHTML(match.label)}</span></td>
      <td class="detail-cell"><div class="detail-cell-inner"><span title="${escapeAttr(item.reason || "")}">${escapeHTML(item.reason || "查看详情")}</span><button class="row-open-button" type="button" aria-label="打开 ${escapeAttr(item.ticker)} 详情">${icon("chevron-right")}</button></div></td>
    </tr>`;
}

function valueCell(item) {
  if (item.net_cash_flow !== null && item.net_cash_flow !== undefined) {
    return { text: money(item.net_cash_flow), className: Number(item.net_cash_flow) >= 0 ? "number-positive" : "number-negative" };
  }
  const snapshot = item.bucket === "window_outside_closest"
    ? (item.all_day_closest || item.latest_priced || item.latest)
    : (item.buy_window_best || item.all_day_closest || item.latest_priced || item.latest);
  if (snapshot?.current_gain_pct !== null && snapshot?.current_gain_pct !== undefined) {
    const gap = snapshot.drop_gap_pct;
    const detail = gap > 0 ? `${percent(snapshot.current_gain_pct)} · 差 ${percent(gap)}` : percent(snapshot.current_gain_pct);
    return { text: detail, className: gap > 0 ? "number-warning" : "number-positive" };
  }
  return { text: "—", className: "" };
}

function renderTimeline() {
  let events = [...(state.data?.timeline || [])];
  if (state.timelineDescending) events.reverse();
  el["timeline-order"].innerHTML = icon(state.timelineDescending ? "up" : "down");
  el["timeline-order"].title = state.timelineDescending ? "当前从晚到早" : "当前从早到晚";
  el["timeline-empty"].hidden = events.length > 0;
  el["timeline-list"].hidden = events.length === 0;
  el["timeline-list"].innerHTML = events.slice(0, 80).map((item) => `
    <li class="timeline-item severity-${severityClass(item.severity)}" ${item.symbol ? `data-symbol="${escapeAttr(item.symbol)}" tabindex="0" role="button"` : ""}>
      <time class="timeline-time" datetime="${escapeAttr(item.occurred_at || "")}">${formatTime(item.occurred_at)}</time>
      <div class="timeline-event"><strong>${escapeHTML(item.title)}</strong><span title="${escapeAttr(item.detail || "")}">${escapeHTML(item.detail || sourceName(item.source))}</span></div>
    </li>`).join("");
  el["timeline-list"].querySelectorAll("[data-symbol]").forEach((node) => {
    node.addEventListener("click", () => openDrawer(node.dataset.symbol, node));
    node.addEventListener("keydown", (event) => { if (event.key === "Enter") openDrawer(node.dataset.symbol, node); });
  });
}

function renderAttention() {
  const items = state.data?.attention || [];
  el["attention-count"].textContent = String(items.length);
  el["attention-empty"].hidden = items.length > 0;
  el["attention-list"].hidden = items.length === 0;
  el["attention-list"].innerHTML = items.map((item) => `
    <li class="attention-item severity-${severityClass(item.severity)}">
      ${icon(item.severity === "critical" ? "alert" : "info")}
      <div><strong>${escapeHTML(item.title)}</strong><p>${escapeHTML(item.message)}</p><button type="button" data-attention="${escapeAttr(item.code)}">${escapeHTML(item.action_label || "查看相关证据")}</button></div>
    </li>`).join("");
  el["attention-list"].querySelectorAll("button").forEach((button) => button.addEventListener("click", () => {
    if (button.dataset.attention === "BROKER_ACTIVITY_NOT_IN_LOCAL_LEDGER") {
      state.bucket = "broker_activity";
      renderMetrics(); renderQuickFilters(); renderDecisionTable();
      el["decision-workspace"].scrollIntoView({ behavior: "smooth" });
    } else {
      el["health-content"].scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }));
}

function renderFunnel() {
  const funnel = state.data?.funnel || {};
  const rows = [
    ["观察池（所有入选）", funnel.observed || 0],
    ["窗口内最接近", funnel.window_near || 0],
    ["本地下单", funnel.local_submitted || 0],
    ["本地成交", funnel.local_filled || 0],
  ];
  el["funnel-content"].innerHTML = `<div class="funnel-stack">${rows.map(([label, value]) => `<div class="funnel-row"><span>${escapeHTML(label)}</span><strong>${value}</strong></div>`).join("")}</div><div class="generic-kv" style="margin-top:8px"><div class="generic-kv-row"><dt>规则排除</dt><dd class="number-warning">${num(funnel.excluded)}</dd></div></div>`;
}

function renderLifecycle() {
  const s = state.data?.summary || {};
  el["lifecycle-content"].innerHTML = `
    <div class="lifecycle-strip">
      <div class="lifecycle-node severity-success"><span class="lifecycle-node-icon">${icon("up")}</span><span>买入 ${num(s.broker_bought_symbols)}</span></div>
      <span class="lifecycle-arrow">→</span>
      <div class="lifecycle-node severity-critical"><span class="lifecycle-node-icon">${icon("down")}</span><span>已卖出 ${num(s.broker_closed_symbols)}</span></div>
      <span class="lifecycle-arrow">→</span>
      <div class="lifecycle-node severity-info"><span class="lifecycle-node-icon">${icon("wallet")}</span><span>持仓 ${num(s.current_positions)}</span></div>
    </div>
    <div class="cash-flow-box"><span>当日成交净现金流</span><strong class="${cashTone(s.net_cash_flow) === "success" ? "number-positive" : "number-negative"}">${escapeHTML(money(s.net_cash_flow))}</strong><small>按成交额计算，未含费用；不等同券商已实现盈亏。</small></div>`;
}

function renderReasons() {
  const items = state.data?.reason_distribution || [];
  const max = Math.max(1, ...items.map((item) => item.count || 0));
  el["reasons-content"].innerHTML = items.length ? `<div class="reason-list">${items.map((item) => `
    <button class="reason-row text-button" type="button" data-reason="${escapeAttr(item.code)}">
      <span class="reason-label"><span>${escapeHTML(item.label)}</span><i class="reason-bar"><i style="width:${Math.round((item.count / max) * 100)}%"></i></i></span>
      <strong class="reason-count">${item.count}</strong>
    </button>`).join("")}</div>` : emptyText("没有可统计的未买原因");
  el["reasons-content"].querySelectorAll("[data-reason]").forEach((button) => button.addEventListener("click", () => {
    state.reason = button.dataset.reason;
    el["reason-filter"].value = state.reason;
    renderDecisionTable();
    el["decision-workspace"].scrollIntoView({ behavior: "smooth" });
  }));
}

function renderOrders() {
  const orders = state.data?.orders || [];
  el["orders-count"].textContent = `${orders.length} 笔`;
  if (!orders.length) {
    el["orders-content"].innerHTML = emptyText("当天没有可用订单记录");
    return;
  }
  el["orders-content"].innerHTML = `<div class="table-wrap"><table class="compact-order-table"><thead><tr><th>时间</th><th>来源</th><th>代码</th><th>方向</th><th>状态</th><th>成交</th></tr></thead><tbody>${orders.slice(0, 12).map((order) => `
    <tr><td>${formatTime(order.filled_at || order.canceled_at || order.submitted_at || order.created_at)}</td><td>${escapeHTML(sourceName(order.source))}</td><td>${escapeHTML((order.ticker || order.symbol || "").replace("US.", ""))}</td><td>${order.side === "BUY" ? "买" : "卖"}</td><td>${escapeHTML(order.status || "—")}</td><td>${float(order.filled_qty) > 0 ? `${formatQty(order.filled_qty)} @ ${price(order.filled_avg_price)}` : "—"}</td></tr>`).join("")}</tbody></table></div>`;
}

function renderPhases() {
  const phases = state.data?.phases || [];
  el["phases-content"].innerHTML = phases.length ? `<div class="phase-list">${phases.map((phase) => `
    <div class="phase-row"><span class="phase-icon">${icon(phase.phase === "intraday" ? "chart" : "clock")}</span><span class="phase-main"><strong>${escapeHTML(phase.label)} · ${escapeHTML(phase.mode)}</strong><span>${escapeHTML(phase.coverage)} · ${num(phase.round_count)} 轮</span></span><span class="phase-value">${num(phase.symbol_count)} 只</span></div>`).join("")}</div>` : emptyText("没有三时段覆盖数据");
}

function renderHealth() {
  const sources = state.data?.sources || [];
  const broker = state.data?.broker || {};
  const rows = sources.map((source) => ({ label: source.label, detail: `${source.file} · ${bytes(source.bytes)} · ${source.modified_at ? formatDateTime(source.modified_at) : "未生成"}`, value: source.status, status: source.status === "healthy" || source.status === "present" ? "success" : (source.status === "empty" ? "warning" : "critical") }));
  rows.push({ label: "Alpaca Trading API（只读）", detail: broker.synced_at ? `同步 ${formatDateTime(broker.synced_at)}` : (broker.error || "尚未核对"), value: broker.status || "not_requested", status: broker.status === "verified" ? "success" : "warning" });
  el["health-content"].innerHTML = `<div class="health-list">${rows.map((row) => `
    <div class="health-row"><span class="health-icon severity-${row.status}">${icon(row.status === "success" ? "check" : "alert")}</span><span class="health-main"><strong>${escapeHTML(row.label)}</strong><span>${escapeHTML(row.detail)}</span></span><span class="health-value severity-${row.status}">${escapeHTML(healthStatus(row.value))}</span></div>`).join("")}</div>`;
}

function openDrawer(symbol, focusSource) {
  const item = (state.data?.symbols || []).find((entry) => entry.symbol === symbol);
  if (!item) return;
  state.selectedSymbol = symbol;
  state.lastFocused = focusSource || document.activeElement;
  document.body.classList.add("drawer-open");
  el["drawer-backdrop"].hidden = false;
  el["symbol-drawer"].hidden = false;
  renderDecisionTable();
  setDrawerTab("lifecycle");
  renderDrawer();
  requestAnimationFrame(() => el["drawer-close"].focus());
}

function closeDrawer() {
  if (el["symbol-drawer"].hidden) return;
  document.body.classList.remove("drawer-open");
  el["drawer-backdrop"].hidden = true;
  el["symbol-drawer"].hidden = true;
  el["drawer-evidence-context"].hidden = true;
  const previous = state.lastFocused;
  state.selectedSymbol = null;
  renderDecisionTable();
  if (previous?.focus) requestAnimationFrame(() => previous.focus());
}

function renderDrawer() {
  const item = selectedSymbol();
  if (!item) return;
  el["drawer-title"].textContent = `${item.ticker} · ${item.status_label}`;
  el["drawer-subtitle"].textContent = item.reason || "查看该股的成交、策略与证据。";
  const stats = [
    ["买入", item.buy_filled_qty ? `${formatQty(item.buy_filled_qty)} 股` : "—"],
    ["买入均价", price(item.buy_avg_price)],
    ["卖出", item.sell_filled_qty ? `${formatQty(item.sell_filled_qty)} 股` : "—"],
    ["卖出均价", price(item.sell_avg_price)],
    ["成交净现金流", money(item.net_cash_flow)],
    ["当前持仓", `${formatQty(item.current_position_qty || 0)} 股`],
  ];
  el["drawer-stats"].innerHTML = stats.map(([label, value]) => `<div class="drawer-stat"><span>${escapeHTML(label)}</span><strong class="${label.includes("现金流") ? (float(item.net_cash_flow) >= 0 ? "number-positive" : "number-negative") : ""}">${escapeHTML(value)}</strong></div>`).join("");
  renderDrawerTimeline(item);
  renderDrawerChecklist(item);
  renderDrawerConsistency(item);
  renderDrawerStrategy(item);
  renderDrawerEvidence(item);
  updateDrawerNavigation();
  el["show-symbol-orders"].querySelector("span").textContent = `查看全部 ${item.ticker} 订单`;
}

function renderDrawerTimeline(item) {
  const orderEvents = (item.orders || []).map((order) => ({
    time: order.filled_at || order.canceled_at || order.submitted_at,
    title: `${order.side || ""} ${order.order_type || ""} · ${order.status || ""}`,
    detail: `${formatQty(order.filled_qty || order.qty || 0)} @ ${price(order.filled_avg_price || order.limit_price)}`,
    status: float(order.filled_qty) > 0 ? "success" : "warning",
  }));
  const positionEvents = (item.position_events || []).map((event) => ({ time: event.occurred_at, title: event.label, detail: `Monitor Log · 置信度 ${event.confidence}`, status: "info" }));
  const events = [...orderEvents, ...positionEvents].sort((a, b) => String(a.time || "").localeCompare(String(b.time || "")));
  el["drawer-order-timeline"].innerHTML = events.length ? events.map((event) => `
    <li class="order-event"><time>${formatTime(event.time)}</time><span class="order-event-main"><strong>${escapeHTML(event.title)}</strong><span>${escapeHTML(event.detail)}</span></span><span class="order-event-status severity-${event.status}">${event.status === "success" ? "已确认" : (event.status === "info" ? "已观察" : "未成交")}</span></li>`).join("") : `<li class="order-event"><span class="order-event-main"><strong>当天无订单生命周期</strong><span>该股仅有策略观察或本地证据。</span></span></li>`;
}

function renderDrawerChecklist(item) {
  const checks = [
    [item.buy_filled_qty > 0, "Alpaca 买入成交已确认"],
    [(item.position_events || []).some((event) => ["added_observed", "existing_at_open"].includes(event.event_type)), "监控日志观察到持仓"],
    [item.sell_filled_qty > 0, "Alpaca 卖出成交已确认"],
    [float(item.current_position_qty) === 0, "当前券商持仓为 0"],
    [item.local_ledger_match === "matched", item.local_ledger_match === "missing" ? "本地订单账本缺失" : `本地账本${matchLabel(item.local_ledger_match).label}`],
    [false, "下单来源/操作者/策略需要以 order_id 继续归因"],
  ];
  el["drawer-checklist"].innerHTML = checks.map(([ok, label]) => `<li class="${ok ? "severity-success" : "severity-warning"}">${icon(ok ? "check" : "alert")}<span>${escapeHTML(label)}</span></li>`).join("");
}

function renderDrawerConsistency(item) {
  const brokerCount = (item.orders || []).filter((order) => order.source === "alpaca").length;
  const localState = state.data?.summary?.local_order_file_state || "missing";
  const monitorCount = (item.position_events || []).length;
  const matchedCount = Number(item.local_ledger_matched_order_count || 0);
  const brokerIdCount = Number(item.broker_order_id_count || brokerCount);
  el["drawer-consistency"].innerHTML = `<table class="consistency-table"><thead><tr><th>数据源</th><th>可用性</th><th>${escapeHTML(item.ticker)} 相关记录</th><th>核对状态</th></tr></thead><tbody>
    <tr><td>Local ledger</td><td>${escapeHTML(healthStatus(localState))}</td><td>${matchedCount} / ${brokerIdCount} 笔订单 ID</td><td class="${matchLabel(item.local_ledger_match).className}">${escapeHTML(matchLabel(item.local_ledger_match).label)}</td></tr>
    <tr><td>Alpaca API</td><td>${state.data?.broker?.status === "verified" ? "完整" : "不可用"}</td><td>${brokerCount} 笔订单</td><td>${state.data?.broker?.status === "verified" ? "已核对" : "待核对"}</td></tr>
    <tr><td>Monitor Log</td><td>${monitorCount ? "可用" : "无相关变化"}</td><td>${monitorCount} 条持仓事件</td><td>${monitorCount ? "部分一致" : "仅策略上下文"}</td></tr>
  </tbody></table>`;
}

function renderDrawerStrategy(item) {
  const snapshots = [
    ["买入窗口内最佳", item.buy_window_best],
    ["全天最接近", item.all_day_closest],
    ["日终/最后状态", item.latest_priced || item.latest],
  ];
  const membership = (item.source_labels || []).includes("策略观察");
  el["drawer-strategy-context"].innerHTML = `
    <article class="snapshot-card"><h4>归因边界</h4><p class="muted">${membership ? "该股在 MA5 策略观察池内；是否由策略下单仍需匹配本地 order_id。" : "该股不在当天 14 只 MA5 观察池；不能从现有证据确认是本策略下单。"}</p></article>
    ${snapshots.map(([title, snapshot]) => snapshotCard(title, snapshot)).join("")}`;
}

function snapshotCard(title, snapshot) {
  if (!snapshot) return `<article class="snapshot-card"><h4>${escapeHTML(title)}</h4><p class="muted">无有效快照</p></article>`;
  return `<article class="snapshot-card"><h4>${escapeHTML(title)}</h4><dl>
    <div><dt>时间</dt><dd>${formatDateTime(snapshot.observed_at)}</dd></div>
    <div><dt>当前价</dt><dd>${price(snapshot.current_price)}</dd></div>
    <div><dt>当前涨跌</dt><dd>${percent(snapshot.current_gain_pct)}</dd></div>
    <div><dt>动态 MA5</dt><dd>${price(snapshot.ma5)}</dd></div>
    <div><dt>最终买点</dt><dd>${price(snapshot.decision_price)}</dd></div>
    <div><dt>跌幅门槛差</dt><dd>${snapshot.drop_gap_pct === null ? "—" : percent(snapshot.drop_gap_pct)}</dd></div>
    <div><dt>可执行</dt><dd>${snapshot.actionable ? "是" : "否"}</dd></div>
  </dl><p class="muted">${escapeHTML(snapshot.reason || "")}</p></article>`;
}

function renderDrawerEvidence(item) {
  const evidence = [...(item.evidence || [])];
  [item.buy_window_best, item.all_day_closest, item.latest].forEach((snapshot) => {
    if (snapshot?.evidence && !evidence.some((entry) => entry.source_id === snapshot.evidence.source_id && entry.line === snapshot.evidence.line)) evidence.push(snapshot.evidence);
  });
  const sourceMap = new Map((state.data?.sources || []).map((source) => [source.id, source]));
  el["drawer-evidence-list"].innerHTML = evidence.length ? evidence.map((entry, index) => {
    const source = sourceMap.get(entry.source_id) || {};
    return `<li><span>${icon("file")}<span><strong>${escapeHTML(source.label || entry.source_id)}</strong><small>${escapeHTML(source.file || "本地证据")} · 行 ${entry.line || "—"}</small></span></span>${entry.line ? `<button class="text-button" type="button" data-source="${escapeAttr(entry.source_id)}" data-line="${entry.line}">查看上下文</button>` : ""}</li>`;
  }).join("") : `<li><span>${icon("info")}<span><strong>没有可展开的本地证据</strong><small>券商订单仍可在成交闭环中查看。</small></span></span></li>`;
  el["drawer-evidence-list"].querySelectorAll("[data-source]").forEach((button) => button.addEventListener("click", () => loadEvidence(button.dataset.source, button.dataset.line)));
}

async function loadEvidence(source, line) {
  if (!state.data?.review_date || !source || !line) return;
  el["drawer-evidence-context"].hidden = false;
  el["evidence-context-title"].textContent = `${source} · 行 ${line}`;
  el["evidence-context-output"].textContent = "正在读取证据上下文…";
  try {
    const query = new URLSearchParams({ date: state.data.review_date, source, line: String(line) });
    const data = await fetchJSON(`${API.evidence}?${query}`);
    el["evidence-context-title"].textContent = `${data.file} · 行 ${data.line}`;
    el["evidence-context-output"].textContent = (data.lines || []).map((entry) => `${String(entry.line).padStart(6, " ")}  ${entry.text}`).join("\n");
  } catch (error) {
    el["evidence-context-output"].textContent = `证据读取失败：${error.message}`;
  }
}

function setDrawerTab(tabName) {
  state.drawerTab = tabName;
  document.querySelectorAll(".drawer-tabs [data-tab]").forEach((tab) => {
    const selected = tab.dataset.tab === tabName;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  });
  document.querySelectorAll(".drawer-tab-panel").forEach((panel) => { panel.hidden = panel.id !== `panel-${tabName}`; });
}

function handleTabKeys(event) {
  if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
  const tabs = [...document.querySelectorAll(".drawer-tabs [data-tab]")];
  const current = tabs.findIndex((tab) => tab.getAttribute("aria-selected") === "true");
  const next = (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
  setDrawerTab(tabs[next].dataset.tab);
  tabs[next].focus();
}

function moveDrawer(direction) {
  const items = filteredSymbols();
  const index = items.findIndex((item) => item.symbol === state.selectedSymbol);
  if (index < 0) return;
  const next = items[index + direction];
  if (!next) return;
  state.selectedSymbol = next.symbol;
  renderDecisionTable();
  renderDrawer();
}

function updateDrawerNavigation() {
  const items = filteredSymbols();
  const index = items.findIndex((item) => item.symbol === state.selectedSymbol);
  el["drawer-previous"].disabled = index <= 0;
  el["drawer-next"].disabled = index < 0 || index >= items.length - 1;
}

async function copySelectedSummary() {
  const item = selectedSymbol();
  if (!item) return;
  const lines = [
    `${item.ticker} · ${item.status_label}`,
    item.reason,
    `买入成交：${formatQty(item.buy_filled_qty)} 股 @ ${price(item.buy_avg_price)}`,
    `卖出成交：${formatQty(item.sell_filled_qty)} 股 @ ${price(item.sell_avg_price)}`,
    `当日成交净现金流：${money(item.net_cash_flow)}（未含费用，不等同已实现盈亏）`,
    `本地账本一致性：${matchLabel(item.local_ledger_match).label}`,
  ].filter(Boolean).join("\n");
  try {
    await navigator.clipboard.writeText(lines);
    el["copy-status"].textContent = "核对摘要已复制";
    showToast("核对摘要已复制", "success");
  } catch {
    showToast("浏览器不允许写入剪贴板，请手动复制。", "warning");
  }
}

function selectedSymbol() {
  return (state.data?.symbols || []).find((item) => item.symbol === state.selectedSymbol) || null;
}

function filteredSymbols() {
  let items = [...(state.data?.symbols || [])];
  items = items.filter(matchesBucket);
  if (state.status) items = items.filter((item) => item.status_label === state.status);
  if (state.reason) items = items.filter((item) => item.reason_code === state.reason);
  if (state.search) items = items.filter((item) => [item.symbol, item.ticker, item.status_label, item.reason, ...(item.source_labels || [])].join(" ").toLowerCase().includes(state.search));
  items.sort(compareSymbols);
  return items;
}

function matchesBucket(item) {
  switch (state.bucket) {
    case "broker_filled": return ["broker_closed", "broker_bought"].includes(item.bucket);
    case "broker_closed": return item.bucket === "broker_closed";
    case "broker_activity": return (item.orders || []).some((order) => order.source === "alpaca");
    case "buy_unfilled": return item.bucket === "buy_unfilled";
    case "strategy": return ["not_bought", "window_outside_closest", "excluded"].includes(item.bucket);
    case "excluded": return item.bucket === "excluded";
    case "current_position": return float(item.current_position_qty) !== 0;
    case "cash_flow": return item.net_cash_flow !== null && item.net_cash_flow !== undefined;
    case "data_conflict": return ["missing", "partial", "unmatched"].includes(item.local_ledger_match);
    default: return true;
  }
}

function compareSymbols(a, b) {
  const direction = state.sortDirection === "asc" ? 1 : -1;
  const values = {
    priority: [bucketPriority(a.bucket), bucketPriority(b.bucket)],
    symbol: [a.ticker, b.ticker],
    source: [(a.source_labels || []).join(","), (b.source_labels || []).join(",")],
    status: [a.status_label, b.status_label],
    buy: [float(a.buy_filled_qty), float(b.buy_filled_qty)],
    sell: [float(a.sell_filled_qty), float(b.sell_filled_qty)],
    value: [float(a.net_cash_flow ?? a.all_day_closest?.drop_gap_pct ?? 0), float(b.net_cash_flow ?? b.all_day_closest?.drop_gap_pct ?? 0)],
    time: [symbolTime(a), symbolTime(b)],
    match: [a.local_ledger_match, b.local_ledger_match],
  }[state.sortKey] || [a.ticker, b.ticker];
  if (typeof values[0] === "number") return (values[0] - values[1]) * direction;
  return localeCompare(String(values[0] || ""), String(values[1] || "")) * direction;
}

function handleSort(event) {
  const header = event.target.closest("th[data-sort]");
  if (!header) return;
  const key = header.dataset.sort;
  if (state.sortKey === key) state.sortDirection = state.sortDirection === "asc" ? "desc" : "asc";
  else { state.sortKey = key; state.sortDirection = "asc"; }
  renderDecisionTable();
}

function updateSortHeaders() {
  el["decision-table"].querySelectorAll("th[data-sort]").forEach((header) => {
    header.removeAttribute("aria-sort");
    if (header.dataset.sort === state.sortKey) header.setAttribute("aria-sort", state.sortDirection === "asc" ? "ascending" : "descending");
  });
}

function setBucket(bucket) {
  state.bucket = state.bucket === bucket && bucket ? "" : bucket;
  renderMetrics();
  renderQuickFilters();
  renderDecisionTable();
}

function clearFilters() {
  state.bucket = ""; state.status = ""; state.reason = ""; state.search = "";
  el["status-filter"].value = ""; el["reason-filter"].value = ""; el["symbol-search"].value = "";
  renderMetrics(); renderQuickFilters(); renderDecisionTable();
}

function filtersActive() { return Boolean(state.bucket || state.status || state.reason || state.search); }

function renderFilterSummary() {
  const labels = [];
  if (state.bucket) labels.push(`视图：${bucketLabel(state.bucket)}`);
  if (state.status) labels.push(`状态：${state.status}`);
  if (state.reason) labels.push(`原因：${el["reason-filter"].selectedOptions[0]?.textContent || state.reason}`);
  if (state.search) labels.push(`搜索：${state.search}`);
  el["active-filter-summary"].innerHTML = labels.length ? labels.map((label) => `<span>${escapeHTML(label)}</span>`).join("") : "未启用筛选";
}

function syncDateState() {
  const data = state.data;
  if (!data) return;
  if (!state.dates.includes(data.review_date)) state.dates.push(data.review_date);
  state.dates.sort().reverse();
  el["review-date"].value = data.review_date;
  const url = new URL(location.href);
  url.searchParams.set("date", data.review_date);
  history.replaceState({}, "", url);
}

function updateDateButtons() {
  const dateValue = state.data?.review_date;
  const index = state.dates.indexOf(dateValue);
  el["previous-date"].disabled = index < 0 || index >= state.dates.length - 1;
  el["next-date"].disabled = index <= 0;
}

function navigateDate(direction) {
  const index = state.dates.indexOf(state.data?.review_date);
  const target = direction === "previous" ? state.dates[index + 1] : state.dates[index - 1];
  if (target) loadReview(target);
}

function setBusy(busy) {
  el["refresh-button"].disabled = busy;
  el["refresh-button"].classList.toggle("is-loading", busy);
  el["review-date"].disabled = busy;
  if (busy) announce("正在加载当天复盘");
}

function showError(message) {
  el["page-error"].hidden = false;
  el["page-error-message"].textContent = message;
  el["metric-rail"].setAttribute("aria-busy", "false");
}
function hideError() { el["page-error"].hidden = true; }

async function fetchJSON(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store" });
  let payload;
  try { payload = await response.json(); } catch { payload = null; }
  if (!response.ok) throw new Error(payload?.error || `HTTP ${response.status}`);
  return payload;
}

function handleGlobalKeys(event) {
  if (event.key === "/" && !isTextInput(event.target) && el["symbol-drawer"].hidden) {
    event.preventDefault(); el["symbol-search"].focus();
  }
  if (!el["symbol-drawer"].hidden) {
    if (event.key === "Escape") { event.preventDefault(); closeDrawer(); }
    if (event.key === "ArrowUp" && !isTextInput(event.target)) { event.preventDefault(); moveDrawer(-1); }
    if (event.key === "ArrowDown" && !isTextInput(event.target)) { event.preventDefault(); moveDrawer(1); }
    if (event.key === "Tab") trapDrawerFocus(event);
  }
}

function trapDrawerFocus(event) {
  const focusable = [...el["symbol-drawer"].querySelectorAll("button:not([disabled]), a[href], [tabindex]:not([tabindex='-1'])")].filter((node) => !node.hidden && node.offsetParent !== null);
  if (!focusable.length) return;
  const first = focusable[0], last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}

function showToast(message, tone = "info") {
  const node = document.createElement("div");
  node.className = `toast ${tone}`;
  node.textContent = message;
  el["toast-region"].appendChild(node);
  requestAnimationFrame(() => node.classList.add("show"));
  setTimeout(() => { node.classList.remove("show"); setTimeout(() => node.remove(), 220); }, 3600);
}

function announce(message) { el["app-live-region"].textContent = ""; requestAnimationFrame(() => { el["app-live-region"].textContent = message; }); }

function setSelectOptions(select, allLabel, options, selected) {
  select.innerHTML = `<option value="">${escapeHTML(allLabel)}</option>${options.map(([value, label]) => `<option value="${escapeAttr(value)}">${escapeHTML(label)}</option>`).join("")}`;
  select.value = options.some(([value]) => value === selected) ? selected : "";
  if (!select.value && selected) {
    if (select === el["status-filter"]) state.status = "";
    if (select === el["reason-filter"]) state.reason = "";
  }
}

function matchLabel(value) {
  const values = {
    matched: { label: "已匹配", className: "number-positive", icon: "check" },
    missing: { label: "本地无记录", className: "number-negative", icon: "alert" },
    partial: { label: "部分匹配", className: "number-warning", icon: "alert" },
    unmatched: { label: "未匹配", className: "number-warning", icon: "alert" },
    not_applicable: { label: "策略证据", className: "number-info", icon: "info" },
  };
  return values[value] || values.not_applicable;
}

function symbolTime(item) {
  const orderTimes = (item.orders || []).map((order) => order.filled_at || order.canceled_at || order.submitted_at).filter(Boolean).sort();
  if (orderTimes.length) return orderTimes.length > 1 ? `${formatTime(orderTimes[0])} | ${formatTime(orderTimes.at(-1))}` : formatTime(orderTimes[0]);
  const snapshot = item.bucket === "window_outside_closest" ? item.all_day_closest : (item.buy_window_best || item.all_day_closest || item.latest);
  return formatTime(snapshot?.observed_at);
}

function sourceName(value) { return value === "alpaca" ? "Alpaca API" : value === "monitor_auto" ? "监控日志" : value === "buy_exclusions" ? "排除记录" : value === "local" ? "本地账本" : String(value || "本地证据"); }
function healthStatus(value) { return ({ healthy: "正常", present: "正常", empty: "空文件", missing: "缺失", verified: "已核对", unavailable: "不可用", not_requested: "未核对" })[value] || String(value || "未知"); }
function bucketPriority(value) { return ({ broker_closed: 0, broker_bought: 1, buy_unfilled: 2, position_unreconciled: 3, excluded: 4, window_outside_closest: 5, not_bought: 6, broker_activity: 7 })[value] ?? 99; }
function bucketLabel(value) { return ({ broker_filled: "券商已成交", broker_closed: "已全部卖出", broker_activity: "券商订单", buy_unfilled: "买入未成", strategy: "策略未买", excluded: "今日排除", current_position: "当前持仓", cash_flow: "有成交净现金流", data_conflict: "数据冲突" })[value] || "全部"; }
function severityClass(value) { return ["success", "warning", "critical", "info", "neutral"].includes(value) ? value : "neutral"; }
function cashTone(value) { return value === null || value === undefined ? "neutral" : (Number(value) >= 0 ? "success" : "danger"); }
function money(value) { return value === null || value === undefined || Number.isNaN(Number(value)) ? "—" : `${Number(value) >= 0 ? "+" : "-"}$${Math.abs(Number(value)).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`; }
function percent(value) { return value === null || value === undefined || Number.isNaN(Number(value)) ? "—" : `${Number(value) >= 0 ? "+" : ""}${(Number(value) * 100).toFixed(2)}%`; }
function price(value) { return value === null || value === undefined || Number.isNaN(Number(value)) ? "—" : Number(value).toLocaleString("en-US", { minimumFractionDigits: Number(value) < 1 ? 4 : 2, maximumFractionDigits: 6 }); }
function formatQty(value) { return Number(value || 0).toLocaleString("en-US", { maximumFractionDigits: 6 }); }
function num(value) { return Number(value || 0).toLocaleString("zh-CN"); }
function float(value) { const number = Number(value); return Number.isFinite(number) ? number : 0; }
function bytes(value) { const number = Number(value || 0); if (!number) return "0 B"; const units = ["B", "KB", "MB", "GB"]; const index = Math.min(Math.floor(Math.log(number) / Math.log(1024)), units.length - 1); return `${(number / (1024 ** index)).toFixed(index ? 1 : 0)} ${units[index]}`; }
function formatTime(value) { if (!value) return "—"; const date = new Date(value); return Number.isNaN(date.getTime()) ? String(value).slice(11, 16) : new Intl.DateTimeFormat("zh-CN", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hour12: false }).format(date); }
function formatDateTime(value) { if (!value) return "—"; const date = new Date(value); return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat("zh-CN", { timeZone: "America/New_York", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(date); }
function validDate(value) { return /^\d{4}-\d{2}-\d{2}$/.test(String(value || "")); }
function localeCompare(a, b) { return a.localeCompare(b, "zh-CN", { numeric: true, sensitivity: "base" }); }
function emptyText(text) { return `<div class="section-empty compact-empty">${icon("info")}<span>${escapeHTML(text)}</span></div>`; }
function isTextInput(target) { return target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement || target?.isContentEditable; }
function icon(name) { return `<svg aria-hidden="true"><use href="#icon-${escapeAttr(name)}"></use></svg>`; }
function escapeHTML(value) { return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]); }
function escapeAttr(value) { return escapeHTML(value).replace(/`/g, "&#96;"); }
