"use strict";

const API = {
  dates: "/api/review/dates",
  review: "/api/review",
  evidence: "/api/review/evidence",
  runtime: "/api/runtime/tasks",
  actionStatus: "/api/actions/status",
  generateWatchcode: "/api/actions/generate-watchcode",
  startMonitor: "/api/actions/start-monitor",
  generatePremarketWatchcode: "/api/actions/generate-premarket-watchcode",
  startPremarketMonitor: "/api/actions/start-premarket-monitor",
  stopMonitor: "/api/actions/stop-monitor",
};

const RUNTIME_POLL = Object.freeze({
  burst: [120, 180, 280, 420, 650, 900, 1200, 1600],
  active: 1000,
  idle: 3000,
  hidden: 10000,
});
const RUNTIME_LINK_PATTERN = /https?:\/\/[^\s<>"'`]+|[A-Za-z]:\\[^\r\n]*?\\watch_code_daily_kline_(?:latest|\d{4}-\d{2}-\d{2})\.html|watch_code_daily_kline_(?:latest|\d{4}-\d{2}-\d{2})\.html/gi;
const WATCHCODE_CHART_NAME_PATTERN = /watch_code_daily_kline_(?:latest|\d{4}-\d{2}-\d{2})\.html/i;

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
  runtimeTasks: [],
  runtimePayload: null,
  selectedRuntime: null,
  runtimeTaskListMarkup: "",
  runtimeFollow: true,
  runtimeView: "events",
  runtimeEventFilter: "",
  runtimeFingerprint: "",
  runtimeLoading: false,
  runtimeTimer: null,
  runtimeFailureCount: 0,
  runtimeBurstAction: "",
  runtimeBurstUntil: 0,
  runtimeBurstIndex: 0,
  pendingRuntimeTask: null,
  actionStatus: null,
  actionStatusError: false,
  actionLoading: "",
  viewDate: "",
  mode: "smart",
  resolvedMode: "review",
  runtimeWasActive: false,
};

const el = {};

document.addEventListener("DOMContentLoaded", init);

async function init() {
  cacheElements();
  initializeViewState();
  bindEvents();
  renderMetricSkeletons();
  renderDailyReadiness();
  loadRuntimeTasks();
  try {
    const response = await fetchJSON(API.dates);
    state.dates = Array.isArray(response.dates) ? response.dates : [];
  } catch (error) {
    showToast(`日期列表读取失败：${error.message}`, "warning");
  }
  const urlDate = new URL(location.href).searchParams.get("date");
  const initialDate = validDate(urlDate) ? urlDate : todayISO();
  await loadReview(initialDate);
}

function cacheElements() {
  const ids = [
    "menu-button", "section-nav", "previous-date", "next-date", "today-button", "review-date", "review-status",
    "coverage-status", "broker-status", "refresh-button", "chart-link", "market-banner", "headline-title",
    "headline-detail", "generated-at", "daily-readiness", "daily-readiness-title", "daily-readiness-overall",
    "daily-readiness-items", "tomorrow-plan-date", "tomorrow-plan-text", "conflict-banner", "conflict-title", "conflict-detail",
    "jump-to-attention", "metric-rail", "page-error", "page-error-message", "retry-button", "decision-workspace",
    "mode-switcher", "data-context-title", "data-context-detail", "freshness-strip", "freshness-page",
    "freshness-runtime", "freshness-broker", "freshness-source", "freshness-environment",
    "runtime-shell", "runtime-shell-status", "runtime-dashboard", "runtime-status", "runtime-summary", "premarket-watchcode-status", "watchcode-status",
    "generate-premarket-watchcode", "start-premarket-monitor", "generate-watchcode", "start-monitor", "stop-monitor",
    "runtime-follow", "runtime-refresh", "runtime-task-list",
    "runtime-view-switcher", "runtime-events-panel", "runtime-console-panel", "runtime-event-filter",
    "runtime-event-summary", "runtime-event-list", "runtime-console-title", "runtime-updated-at", "runtime-console",
    "runtime-link-bar", "runtime-links",
    "decision-count", "clear-filters", "quick-filters", "status-filter", "reason-filter", "symbol-search",
    "filter-summary-button", "active-filter-summary", "decision-table", "decision-table-body", "decision-empty",
    "filtered-total", "timeline-list", "timeline-order", "timeline-empty", "attention-panel", "attention-title", "attention-count",
    "attention-list", "attention-empty", "funnel-content", "lifecycle-content", "reasons-title", "reasons-content", "orders-content",
    "orders-count", "phases-content", "health-content", "drawer-backdrop", "symbol-drawer", "drawer-previous",
    "drawer-next", "drawer-close", "drawer-title", "drawer-subtitle", "drawer-stats", "drawer-order-timeline",
    "drawer-checklist", "drawer-consistency", "drawer-strategy-context", "drawer-evidence-list",
    "drawer-evidence-context", "evidence-context-title", "evidence-context-output", "close-evidence-context",
    "show-symbol-orders", "copy-summary", "open-first-evidence", "copy-status", "toast-region", "app-live-region",
  ];
  ids.forEach((id) => { el[id] = document.getElementById(id); });
}

function bindEvents() {
  el["mode-switcher"].addEventListener("click", (event) => {
    const button = event.target.closest("button[data-mode]");
    if (button) setWorkspaceMode(button.dataset.mode);
  });
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
  el["today-button"].addEventListener("click", () => loadReview(todayISO()));
  el["refresh-button"].addEventListener("click", () => loadReview(state.viewDate || el["review-date"].value));
  el["retry-button"].addEventListener("click", () => loadReview(el["review-date"].value));
  el["runtime-refresh"].addEventListener("click", () => {
    window.clearTimeout(state.runtimeTimer);
    loadRuntimeTasks();
  });
  el["generate-watchcode"].addEventListener("click", () => runDashboardAction("generate-watchcode"));
  el["start-monitor"].addEventListener("click", () => runDashboardAction("start-monitor"));
  el["generate-premarket-watchcode"].addEventListener("click", () => runDashboardAction("generate-premarket-watchcode"));
  el["start-premarket-monitor"].addEventListener("click", () => runDashboardAction("start-premarket-monitor"));
  el["stop-monitor"].addEventListener("click", () => runDashboardAction("stop-monitor"));
  el["runtime-follow"].addEventListener("change", () => {
    state.runtimeFollow = el["runtime-follow"].checked;
    if (state.runtimeFollow) scrollRuntimeConsole();
  });
  el["runtime-task-list"].addEventListener("click", (event) => {
    const task = event.target.closest("button[data-runtime-id]");
    if (!task) return;
    state.selectedRuntime = task.dataset.runtimeId;
    renderRuntimeDashboard();
  });
  el["runtime-view-switcher"].addEventListener("click", (event) => {
    const button = event.target.closest("button[data-runtime-view]");
    if (!button) return;
    state.runtimeView = button.dataset.runtimeView;
    renderRuntimeView();
  });
  el["runtime-event-filter"].addEventListener("change", () => {
    state.runtimeEventFilter = el["runtime-event-filter"].value;
    renderRuntimeEvents(selectedRuntimeTask());
  });
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
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) triggerRuntimeRefresh(0);
  });
  window.addEventListener("online", () => triggerRuntimeRefresh(0));
}

function initializeViewState() {
  const requestedMode = new URL(location.href).searchParams.get("mode");
  state.mode = ["smart", "live", "review"].includes(requestedMode) ? requestedMode : "smart";
}

async function loadRuntimeTasks() {
  if (state.runtimeLoading) {
    scheduleRuntimeRefresh(100);
    return;
  }
  state.runtimeLoading = true;
  try {
    const payload = await fetchJSON(API.runtime, { timeoutMs: 5000 });
    state.runtimePayload = payload;
    state.runtimeTasks = Array.isArray(payload.tasks) ? payload.tasks : [];
    state.runtimeFailureCount = 0;
    reconcilePendingRuntimeTask(payload);
    const displayTasks = runtimeTasksForDisplay();
    const selectedTask = displayTasks.find((task) => task.instance_id === state.selectedRuntime);
    const preferredRunningTask = displayTasks.find((task) => ["running", "starting"].includes(task.status));
    if (!selectedTask) {
      state.selectedRuntime = preferredRunningTask?.instance_id || displayTasks[0]?.instance_id || null;
    }
    renderRuntimeDashboard(payload);
    renderWorkspaceMode();
    renderFreshness();
    try {
      await loadActionStatus();
    } catch (error) {
      renderActionStatusError(error);
    }
    scheduleRuntimeRefresh(nextRuntimeRefreshDelay(payload.active_count));
  } catch (error) {
    state.runtimeFailureCount += 1;
    el["runtime-status"].className = "status-indicator warning";
    el["runtime-status"].innerHTML = '<span class="status-dot" aria-hidden="true"></span><span>状态读取失败</span>';
    el["runtime-shell-status"].className = "disclosure-status warning";
    el["runtime-shell-status"].innerHTML = '<span class="status-dot" aria-hidden="true"></span>状态读取失败';
    el["runtime-summary"].textContent = `无法读取本机盯盘状态：${error.message}`;
    const retryDelay = Math.min(8000, 500 * (2 ** Math.min(state.runtimeFailureCount - 1, 4)));
    scheduleRuntimeRefresh(retryDelay);
  } finally {
    state.runtimeLoading = false;
  }
}

async function loadActionStatus() {
  state.actionStatus = await fetchJSON(API.actionStatus, { timeoutMs: 5000 });
  state.actionStatusError = false;
  renderActionStatus();
}

function renderActionStatus() {
  const status = state.actionStatus || {};
  const watchcode = status.watchcode || {};
  const generating = Boolean(status.intraday_generator_running || status.pending_actions?.includes("generate-watchcode"));
  const premarketGenerating = false;
  const anyGenerating = Boolean(status.generator_running || generating || premarketGenerating);
  const startingMonitor = Boolean(status.pending_actions?.includes("start-monitor"));
  const startingPremarketMonitor = Boolean(status.pending_actions?.includes("start-premarket-monitor"));
  const monitorRunning = Boolean(status.monitor_running);
  const premarketMonitorRunning = Boolean(status.premarket_monitor_running);
  const stoppable = monitorRunning || anyGenerating || startingMonitor || startingPremarketMonitor;
  el["premarket-watchcode-status"].className = "runtime-control-status ready";
  el["premarket-watchcode-status"].querySelector("span:last-child").textContent = "盘前：仅监控当前持仓，不使用 WatchCode";
  renderWatchcodeStatus("watchcode-status", "盘中", watchcode, generating);
  el["generate-premarket-watchcode"].disabled = true;
  el["generate-watchcode"].disabled = anyGenerating || Boolean(state.actionLoading);
  el["start-premarket-monitor"].disabled = monitorRunning || startingPremarketMonitor || Boolean(state.actionLoading);
  el["start-monitor"].disabled = monitorRunning || startingMonitor || Boolean(state.actionLoading);
  el["stop-monitor"].disabled = !stoppable || Boolean(state.actionLoading);
  el["generate-premarket-watchcode"].classList.toggle("is-loading", state.actionLoading === "generate-premarket-watchcode");
  el["start-premarket-monitor"].classList.toggle("is-loading", state.actionLoading === "start-premarket-monitor");
  el["generate-watchcode"].classList.toggle("is-loading", state.actionLoading === "generate-watchcode");
  el["start-monitor"].classList.toggle("is-loading", state.actionLoading === "start-monitor");
  el["stop-monitor"].classList.toggle("is-loading", state.actionLoading === "stop-monitor");
  el["start-premarket-monitor"].querySelector("span").textContent = premarketMonitorRunning ? "盘前监控运行中" : (startingPremarketMonitor ? "正在启动" : "启动盘前监控");
  el["start-monitor"].querySelector("span").textContent = monitorRunning ? "盯盘运行中" : (startingMonitor ? "正在启动" : "启动自动盯盘");
  el["stop-monitor"].querySelector("span").textContent = state.actionLoading === "stop-monitor" ? "正在结束" : "结束盯盘";
  renderDailyReadiness();
}

function renderDailyReadiness() {
  if (!el["daily-readiness-items"]) return;
  const status = state.actionStatus || {};
  const watchcode = status.watchcode || {};
  const premarketWatchcode = status.premarket_watchcode || {};
  const activeTasks = runtimeTasksForDisplay().filter((task) => ["running", "starting"].includes(task.status));
  const taskState = (taskNames) => {
    const matches = activeTasks.filter((task) => taskNames.includes(task.task_name));
    return {
      running: matches.some((task) => task.status === "running"),
      starting: matches.some((task) => task.status === "starting"),
    };
  };
  const premarketTask = taskState(["monitor_premarket"]);
  const intradayTask = taskState(["monitor_auto", "monitor_ma5"]);
  const intradayGenerator = taskState(["watchcode_ma5"]);
  const pendingActions = Array.isArray(status.pending_actions) ? status.pending_actions : [];
  const actionKnown = Boolean(state.actionStatus);
  const runtimeKnown = Boolean(state.runtimePayload);

  const watchcodeItem = (label, data, generating) => {
    if (data.ready) {
      return {
        label,
        stateLabel: "当日已生成",
        detail: `${num(data.symbol_count)} 只 · 信号日 ${data.signal_date || "—"}`,
        done: true,
        tone: "success",
      };
    }
    if (generating) {
      return { label, stateLabel: "正在生成", detail: "当前进度会同步到实时任务输出", tone: "warning", pending: true };
    }
    if (!actionKnown) {
      return {
        label,
        stateLabel: state.actionStatusError ? "状态不可用" : "正在检查",
        detail: state.actionStatusError ? "任务状态读取失败，稍后自动重试" : "正在核对当日文件与信号日",
        tone: "neutral",
      };
    }
    const expected = data.expected_signal_date || "当日信号日";
    const detail = data.exists
      ? `现有信号日 ${data.signal_date || "未知"}，需要更新至 ${expected}`
      : `需要生成信号日 ${expected} 的文件`;
    return { label, stateLabel: "待生成", detail, tone: "warning" };
  };

  const monitorItem = (label, running, starting) => {
    if (running) {
      return { label, stateLabel: "监控已开启", detail: "运行进程已连接到网页看板", done: true, tone: "success" };
    }
    if (starting) {
      return { label, stateLabel: "正在启动", detail: "任务已提交，正在等待进程心跳", tone: "warning", pending: true };
    }
    if (!actionKnown && !runtimeKnown) {
      return {
        label,
        stateLabel: state.actionStatusError ? "状态不可用" : "正在检查",
        detail: state.actionStatusError ? "任务状态读取失败，稍后自动重试" : "正在发现本机运行进程",
        tone: "neutral",
      };
    }
    return { label, stateLabel: "未开启", detail: "当前没有运行中的监控进程", tone: "neutral" };
  };

  const intradayGenerating = Boolean(
    status.intraday_generator_running
      || pendingActions.includes("generate-watchcode")
      || intradayGenerator.running
      || intradayGenerator.starting
  );
  const premarketMonitorRunning = Boolean(status.premarket_monitor_running || premarketTask.running);
  const premarketMonitorStarting = Boolean(pendingActions.includes("start-premarket-monitor") || premarketTask.starting);
  const intradayMonitorRunning = Boolean(
    intradayTask.running
      || (status.monitor_running && !status.premarket_monitor_running)
      || activeTasks.some((task) => task.status === "running" && task.task_name === "monitor_auto")
  );
  const intradayMonitorStarting = Boolean(pendingActions.includes("start-monitor") || intradayTask.starting);
  const items = [
    {
      label: "盘前监控规则",
      stateLabel: "仅当前持仓",
      detail: "不使用 WatchCode；滚动 60 秒涨跌 3% 才提醒",
      done: true,
      tone: "success",
    },
    watchcodeItem("盘中 WatchCode", watchcode, intradayGenerating),
    monitorItem("盘前持仓监控", premarketMonitorRunning, premarketMonitorStarting),
    monitorItem("盘中监控", intradayMonitorRunning, intradayMonitorStarting),
  ];
  const completed = items.filter((item) => item.done).length;
  const pending = items.some((item) => item.pending);
  const known = actionKnown || runtimeKnown;
  const allDone = completed === items.length;

  el["daily-readiness"].setAttribute("aria-busy", String(!known));
  el["daily-readiness-title"].textContent = "今日准备进度";
  el["daily-readiness-overall"].className = `daily-readiness-overall ${allDone ? "success" : pending ? "warning" : "neutral"}`;
  el["daily-readiness-overall"].textContent = !known
    ? "检查中"
    : allDone ? "✓ 全部完成" : `${completed} / ${items.length} 已完成`;
  el["daily-readiness-items"].innerHTML = items.map((item) => `
    <article class="readiness-item ${escapeAttr(item.tone)}">
      <span class="readiness-mark" aria-hidden="true">${item.done ? "✓" : item.pending ? "…" : "—"}</span>
      <div class="readiness-copy">
        <span class="readiness-label">${escapeHTML(item.label)}</span>
        <strong>${escapeHTML(item.stateLabel)}</strong>
        <small>${escapeHTML(item.detail)}</small>
      </div>
    </article>`).join("");

  const tomorrow = addDaysISO(todayISO(), 1);
  const weekend = isoWeekday(tomorrow) === 0 || isoWeekday(tomorrow) === 6;
  el["tomorrow-plan-date"].textContent = formatShortDate(tomorrow, true);
  el["tomorrow-plan-text"].textContent = weekend
    ? "周末通常休市；下一交易日前启动盘前持仓监控、生成盘中 WatchCode、启动自动盯盘。"
    : "启动盘前持仓监控 → 生成盘中 WatchCode → 启动自动盯盘";
}

function renderWatchcodeStatus(elementId, sessionLabel, watchcode, generating) {
  let tone = "critical";
  let label = `${sessionLabel} WatchCode 缺失或过期 · 需要信号日 ${watchcode.expected_signal_date || "—"}`;
  if (generating) {
    tone = "warning";
    label = `正在生成${sessionLabel} WatchCode，输出会显示在下方`;
  } else if (watchcode.ready) {
    tone = "success";
    label = `${sessionLabel} WatchCode 已就绪 · 信号日 ${watchcode.signal_date} · ${num(watchcode.symbol_count)} 只`;
  } else if (watchcode.exists) {
    tone = "warning";
    label = `${sessionLabel} WatchCode 已过期（${watchcode.signal_date || "日期未知"}）· 启动时会先更新`;
  }
  el[elementId].className = `runtime-control-status ${tone}`;
  el[elementId].innerHTML = `<span class="status-dot" aria-hidden="true"></span><span>${escapeHTML(label)}</span>`;
}

function renderActionStatusError(error) {
  state.actionStatus = null;
  state.actionStatusError = true;
  ["premarket-watchcode-status", "watchcode-status"].forEach((elementId) => {
    el[elementId].className = "runtime-control-status warning";
    el[elementId].innerHTML = `<span class="status-dot" aria-hidden="true"></span><span>任务控制状态读取失败：${escapeHTML(error.message)}</span>`;
  });
  renderDailyReadiness();
}

function beginPendingRuntimeTask(action) {
  const definitions = {
    "generate-watchcode": { taskName: "watchcode_ma5", taskLabel: "生成盘中 WatchCode", phase: "prepare" },
    "start-monitor": { taskName: "monitor_auto", taskLabel: "自动盯盘", phase: "startup" },
    "start-premarket-monitor": { taskName: "monitor_premarket", taskLabel: "盘前持仓波动监控", phase: "startup" },
  };
  const definition = definitions[action];
  if (!definition) return;
  const startedAt = new Date().toISOString();
  const instanceId = `pending-${action}-${Date.now()}`;
  state.pendingRuntimeTask = {
    instance_id: instanceId,
    task_name: definition.taskName,
    task_label: definition.taskLabel,
    phase: definition.phase,
    phase_label: "正在提交启动请求",
    status: "starting",
    started_at: startedAt,
    heartbeat_at: startedAt,
    source: "网页控制",
    command: "dashboard action",
    pid: 0,
    log: "启动请求正在提交，左侧任务卡已提前建立。",
    log_truncated: false,
    events: [{
      id: `${instanceId}-submitted`,
      kind: "lifecycle",
      severity: "info",
      symbol: "",
      title: "正在创建任务",
      message: "启动请求正在提交，等待 Python 进程登记实时输出。",
      action: "自动追踪",
      time_label: "刚刚",
      line_number: 0,
      count: 1,
    }],
    pending: true,
  };
  state.selectedRuntime = instanceId;
  startRuntimeBurst(action, 15000);
  renderRuntimeDashboard(state.runtimePayload);
  renderWorkspaceMode();
  triggerRuntimeRefresh(50);
}

function markPendingRuntimeStarted(payload) {
  const pending = state.pendingRuntimeTask;
  if (!pending) return;
  pending.pid = Number(payload?.pid) || 0;
  pending.phase_label = "进程已创建，正在登记输出";
  pending.log = pending.pid
    ? `启动进程 PID ${pending.pid} 已创建，正在等待第一条实时输出…`
    : "启动进程已创建，正在等待第一条实时输出…";
  pending.events[0].title = "进程已创建";
  pending.events[0].message = pending.log;
  renderRuntimeDashboard(state.runtimePayload);
}

function clearPendingRuntimeTask() {
  const pendingId = state.pendingRuntimeTask?.instance_id;
  state.pendingRuntimeTask = null;
  if (state.selectedRuntime === pendingId) {
    state.selectedRuntime = state.runtimeTasks.find((task) => task.status === "running")?.instance_id
      || state.runtimeTasks[0]?.instance_id
      || null;
  }
}

function runtimeTasksForDisplay() {
  return state.pendingRuntimeTask ? [state.pendingRuntimeTask, ...state.runtimeTasks] : state.runtimeTasks;
}

function reconcilePendingRuntimeTask(payload) {
  const pending = state.pendingRuntimeTask;
  if (state.runtimeBurstAction === "stop-monitor" && !Number(payload?.active_count || 0)) {
    endRuntimeBurst();
  }
  if (!pending) return;
  const earliestStart = Date.parse(pending.started_at) - 2000;
  const matchingTask = state.runtimeTasks.find((task) => {
    if (task.status !== "running" || Date.parse(task.started_at || 0) < earliestStart) return false;
    if (state.runtimeBurstAction === "generate-watchcode") return task.task_name === "watchcode_ma5";
    if (state.runtimeBurstAction === "start-premarket-monitor") {
      return task.task_name === "monitor_premarket";
    }
    return ["watchcode_ma5", "monitor_auto", "monitor_ma5"].includes(task.task_name);
  });
  if (matchingTask) {
    clearPendingRuntimeTask();
    state.selectedRuntime = matchingTask.instance_id;
    endRuntimeBurst();
    return;
  }
  if (Date.now() >= state.runtimeBurstUntil) {
    clearPendingRuntimeTask();
    endRuntimeBurst();
  }
}

function startRuntimeBurst(action, durationMs) {
  state.runtimeBurstAction = action;
  state.runtimeBurstUntil = Date.now() + durationMs;
  state.runtimeBurstIndex = 0;
}

function endRuntimeBurst() {
  state.runtimeBurstAction = "";
  state.runtimeBurstUntil = 0;
  state.runtimeBurstIndex = 0;
}

function nextRuntimeRefreshDelay(activeCount) {
  if (document.hidden) return RUNTIME_POLL.hidden;
  if (state.runtimeBurstUntil > Date.now()) {
    const index = Math.min(state.runtimeBurstIndex, RUNTIME_POLL.burst.length - 1);
    state.runtimeBurstIndex += 1;
    return RUNTIME_POLL.burst[index];
  }
  if (state.runtimeBurstAction) endRuntimeBurst();
  return Number(activeCount || 0) > 0 ? RUNTIME_POLL.active : RUNTIME_POLL.idle;
}

function triggerRuntimeRefresh(delay = 0) {
  scheduleRuntimeRefresh(Math.max(0, Number(delay) || 0));
}

async function runDashboardAction(action) {
  if (state.actionLoading) return;
  if (action === "start-monitor") {
    const mode = String(state.data?.broker?.mode || "LIVE").toUpperCase();
    const dependency = state.actionStatus?.watchcode?.ready ? "WatchCode 已就绪。" : "WatchCode 尚未就绪，系统会先生成，成功后再启动。";
    if (!window.confirm(`${dependency}\n\n即将启动 ${mode} 自动盯盘任务，可能执行真实订单。确认继续？`)) return;
  }
  if (action === "start-premarket-monitor") {
    const dependency = "盘前监控不使用 WatchCode，只读取 Alpaca 当前持仓。";
    if (!window.confirm(`${dependency}\n\n仅当持仓在滚动 60 秒内上涨或下跌达到 3% 时提醒，不会提交订单。确认继续？`)) return;
  }
  if (action === "stop-monitor") {
    if (!window.confirm("确认结束当前 MA5 盯盘任务？\n\n正在生成的 WatchCode 也会停止；已经提交到券商的订单不会被撤销。")) return;
  }
  state.actionLoading = action;
  if (["generate-watchcode", "start-monitor", "start-premarket-monitor"].includes(action)) {
    beginPendingRuntimeTask(action);
  }
  if (action === "stop-monitor") {
    startRuntimeBurst(action, 10000);
    triggerRuntimeRefresh(50);
  }
  renderActionStatus();
  try {
    const url = {
      "generate-watchcode": API.generateWatchcode,
      "start-monitor": API.startMonitor,
      "generate-premarket-watchcode": API.generatePremarketWatchcode,
      "start-premarket-monitor": API.startPremarketMonitor,
      "stop-monitor": API.stopMonitor,
    }[action];
    const payload = await fetchJSON(url, { method: "POST", headers: { "X-MA5-Action": "1" }, timeoutMs: 15000 });
    if (payload.status === "started") markPendingRuntimeStarted(payload);
    if (payload.status === "already_running") {
      clearPendingRuntimeTask();
      endRuntimeBurst();
    }
    const fallbackMessage = action === "stop-monitor" ? "盯盘任务已结束。" : "任务已启动。";
    showToast(payload.message || fallbackMessage, ["already_running", "not_running"].includes(payload.status) ? "warning" : "success");
    announce(payload.message || fallbackMessage);
  } catch (error) {
    clearPendingRuntimeTask();
    endRuntimeBurst();
    renderRuntimeDashboard(state.runtimePayload);
    const operation = action === "stop-monitor" ? "结束" : "启动";
    showToast(`任务${operation}失败：${error.message}`, "critical");
  } finally {
    state.actionLoading = "";
    try {
      await loadActionStatus();
    } catch (error) {
      renderActionStatusError(error);
    }
    triggerRuntimeRefresh(0);
  }
}

function scheduleRuntimeRefresh(delay) {
  window.clearTimeout(state.runtimeTimer);
  state.runtimeTimer = window.setTimeout(() => {
    if (document.hidden) {
      scheduleRuntimeRefresh(RUNTIME_POLL.hidden);
      return;
    }
    loadRuntimeTasks();
  }, delay);
}

function renderRuntimeDashboard(payload = null) {
  const tasks = runtimeTasksForDisplay();
  const activeCount = tasks.filter((task) => ["running", "starting"].includes(task.status)).length;
  const runtimeLabel = activeCount ? `${activeCount} 个任务运行中` : "当前无运行任务";
  el["runtime-status"].className = `status-indicator ${activeCount ? "success" : "neutral"}`;
  el["runtime-status"].innerHTML = `<span class="status-dot" aria-hidden="true"></span><span>${runtimeLabel}</span>`;
  el["runtime-shell-status"].className = `disclosure-status ${activeCount ? "success" : "neutral"}`;
  el["runtime-shell-status"].innerHTML = `<span class="status-dot" aria-hidden="true"></span>${runtimeLabel}`;
  el["runtime-summary"].textContent = activeCount
    ? "运行中每 1 秒同步；网页启动后立即显示，并自动追踪真实进程。"
    : "空闲时每 3 秒发现一次任务；网页启动任务会立即显示。";
  renderDailyReadiness();

  if (!tasks.length) {
    const emptyTaskListMarkup = '<div class="runtime-empty"><svg aria-hidden="true"><use href="#icon-terminal"></use></svg><strong>没有发现盯盘任务</strong><span>任务启动后无需额外绑定，网页会自动显示。</span></div>';
    if (state.runtimeTaskListMarkup !== emptyTaskListMarkup) {
      state.runtimeTaskListMarkup = emptyTaskListMarkup;
      el["runtime-task-list"].innerHTML = emptyTaskListMarkup;
    }
    el["runtime-console-title"].textContent = "等待盯盘任务";
    el["runtime-updated-at"].textContent = formatClock(payload?.generated_at);
    renderRuntimeLinks(null);
    renderRuntimeConsole("尚未发现由本项目入口启动的盯盘任务。\n\n支持入口：\n  • monitor_auto.py\n  • monitor_ma5_forever.py\n  • monitor_premarket_ma5.py\n  • monitor_afterhours.py");
    el["runtime-event-summary"].textContent = "当前没有运行中的盯盘任务";
    el["runtime-event-list"].innerHTML = '<div class="runtime-event-empty"><strong>等待状态变化</strong><span>任务启动后，这里优先显示买点、订单、异常与关键阈值变化。</span></div>';
    renderRuntimeView();
    return;
  }

  const taskListMarkup = tasks.map((task) => {
    const selected = task.instance_id === state.selectedRuntime;
    const running = task.status === "running";
    const starting = task.status === "starting";
    const statusLabel = starting ? "正在启动" : running ? "运行中" : task.status === "failed" ? "异常结束" : "已结束";
    const processLabel = task.pid ? `PID ${num(task.pid)}` : "PID 正在分配";
    return `<button class="runtime-task ${task.pending ? "pending" : ""} ${selected ? "selected" : ""}" type="button" role="option" aria-selected="${selected}" data-runtime-id="${escapeAttr(task.instance_id)}">
      <span class="runtime-task-head"><span class="runtime-task-name">${escapeHTML(task.task_label)}</span><span class="runtime-state ${escapeAttr(task.status)}"><span class="status-dot" aria-hidden="true"></span>${statusLabel}</span></span>
      <span class="runtime-task-meta">${escapeHTML(task.phase_label)} · ${processLabel}</span>
      <span class="runtime-task-meta">${escapeHTML(task.source)} · ${formatDateTime(task.started_at)}</span>
    </button>`;
  }).join("");
  if (state.runtimeTaskListMarkup !== taskListMarkup) {
    state.runtimeTaskListMarkup = taskListMarkup;
    el["runtime-task-list"].innerHTML = taskListMarkup;
  }

  const selected = tasks.find((task) => task.instance_id === state.selectedRuntime) || tasks[0];
  el["runtime-console-title"].textContent = `${selected.task_label} · ${selected.phase_label}`;
  el["runtime-updated-at"].textContent = `更新 ${formatClock(payload?.generated_at || selected.heartbeat_at)}`;
  const prefix = selected.log_truncated ? "… 已省略较早输出，仅显示最近内容 …\n\n" : "";
  const log = selected.log || (["running", "starting"].includes(selected.status) ? "任务已启动，等待第一行控制台输出…" : "该任务没有留下控制台输出。");
  const consoleNode = el["runtime-console"];
  const nearBottom = consoleNode.scrollHeight - consoleNode.scrollTop - consoleNode.clientHeight < 48;
  renderRuntimeLinks(selected);
  renderRuntimeConsole(`${prefix}${log}`);
  if (state.runtimeFollow || nearBottom) scrollRuntimeConsole();
  renderRuntimeEvents(selected);
  renderRuntimeView();
}

function selectedRuntimeTask() {
  const tasks = runtimeTasksForDisplay();
  return tasks.find((task) => task.instance_id === state.selectedRuntime) || tasks[0] || null;
}

function runtimeLinkMatches(value) {
  const text = String(value || "");
  const pattern = new RegExp(RUNTIME_LINK_PATTERN.source, RUNTIME_LINK_PATTERN.flags);
  const matches = [];
  for (const match of text.matchAll(pattern)) {
    const token = String(match[0] || "").replace(/[),.;!?，。；：！？】》]+$/u, "");
    const link = runtimeLinkFromToken(token);
    if (!token || !link) continue;
    matches.push({ start: match.index, end: match.index + token.length, token, link });
  }
  return matches;
}

function runtimeLinkFromToken(token) {
  const chartMatch = String(token || "").match(WATCHCODE_CHART_NAME_PATTERN);
  if (chartMatch) {
    const fileName = chartMatch[0].toLowerCase();
    const dateMatch = fileName.match(/\d{4}-\d{2}-\d{2}/);
    return {
      href: `/charts/${encodeURIComponent(fileName)}`,
      label: dateMatch ? `打开 K 线图 · ${dateMatch[0]}` : "打开最新 K 线图",
      kind: "chart",
      source: token,
    };
  }
  if (!/^https?:\/\//i.test(token)) return null;
  try {
    const parsed = new URL(token);
    if (!["http:", "https:"].includes(parsed.protocol)) return null;
    return {
      href: parsed.href,
      label: `打开网页 · ${parsed.hostname}`,
      kind: "web",
      source: token,
    };
  } catch {
    return null;
  }
}

function extractRuntimeLinks(value) {
  const seen = new Set();
  return runtimeLinkMatches(value).map((match) => match.link).filter((link) => {
    if (seen.has(link.href)) return false;
    seen.add(link.href);
    return true;
  });
}

function configureRuntimeAnchor(anchor, link, text) {
  anchor.href = link.href;
  anchor.target = "_blank";
  anchor.rel = "noopener noreferrer";
  anchor.textContent = text;
  anchor.title = link.source === text ? `${link.label}（新窗口）` : `${link.label}\n识别自：${link.source}`;
}

function renderRuntimeLinks(task) {
  const links = extractRuntimeLinks(task?.log);
  el["runtime-links"].replaceChildren();
  el["runtime-link-bar"].hidden = links.length === 0;
  links.forEach((link) => {
    const anchor = document.createElement("a");
    anchor.className = `runtime-link-chip ${link.kind}`;
    configureRuntimeAnchor(anchor, link, `${link.label} ↗`);
    el["runtime-links"].append(anchor);
  });
}

function renderRuntimeConsole(value) {
  const text = String(value || "");
  const matches = runtimeLinkMatches(text);
  if (!matches.length) {
    el["runtime-console"].textContent = text;
    return;
  }
  const fragment = document.createDocumentFragment();
  let cursor = 0;
  matches.forEach((match) => {
    fragment.append(document.createTextNode(text.slice(cursor, match.start)));
    const anchor = document.createElement("a");
    anchor.className = "runtime-console-link";
    configureRuntimeAnchor(anchor, match.link, match.token);
    fragment.append(anchor);
    cursor = match.end;
  });
  fragment.append(document.createTextNode(text.slice(cursor)));
  el["runtime-console"].replaceChildren(fragment);
}

function renderRuntimeEvents(task) {
  if (!task) return;
  const sourceEvents = Array.isArray(task.events) ? task.events : [];
  const filter = state.runtimeEventFilter;
  const events = sourceEvents.filter((event) => {
    if (!filter) return true;
    if (filter === "critical") return event.severity === "critical";
    if (filter === "warning") return event.severity === "warning";
    return filter === "changed" ? event.kind !== "observation" : true;
  });
  const significantCount = sourceEvents.filter((event) => event.kind !== "observation").length;
  const lifecycleLabel = task.status === "starting" ? " · 正在创建进程" : task.status === "running" ? " · 正在监控" : " · 任务已结束";
  el["runtime-event-summary"].innerHTML = `<strong>${events.length}</strong> 条事件 · <span>${significantCount} 条状态变化</span>${lifecycleLabel}`;
  el["runtime-event-list"].innerHTML = events.length ? events.map((event) => `
    <article class="runtime-event severity-${severityClass(event.severity)}">
      <span class="runtime-event-marker" aria-hidden="true"></span>
      <div class="runtime-event-time"><strong>${escapeHTML(event.time_label || "刚刚")}</strong><span>${event.line_number ? `日志 ${num(event.line_number)}` : "实时"}</span></div>
      <div class="runtime-event-main">
        <div class="runtime-event-title"><strong>${escapeHTML(event.symbol || "系统")}</strong><span>${escapeHTML(event.title || "状态更新")}</span>${event.count > 1 ? `<em>×${num(event.count)}</em>` : ""}</div>
        <p>${escapeHTML(event.message || "—")}</p>
      </div>
      <div class="runtime-event-action">${escapeHTML(event.action || "继续观察")}</div>
    </article>`).join("") : '<div class="runtime-event-empty"><strong>没有符合筛选条件的事件</strong><span>可以切换到“全部事件”或查看原始日志。</span></div>';

  const latestImportant = sourceEvents.find((event) => ["critical", "warning"].includes(event.severity));
  const fingerprint = latestImportant ? `${task.instance_id}:${latestImportant.id}` : "";
  if (state.runtimeFingerprint && fingerprint && fingerprint !== state.runtimeFingerprint) {
    announce(`${latestImportant.symbol || "系统"}：${latestImportant.title}`);
  }
  state.runtimeFingerprint = fingerprint;
}

function renderRuntimeView() {
  const showEvents = state.runtimeView === "events";
  el["runtime-events-panel"].hidden = !showEvents;
  el["runtime-console-panel"].hidden = showEvents;
  el["runtime-view-switcher"].querySelectorAll("[data-runtime-view]").forEach((button) => {
    button.setAttribute("aria-selected", String(button.dataset.runtimeView === state.runtimeView));
  });
  if (!showEvents && state.runtimeFollow) scrollRuntimeConsole();
}

function scrollRuntimeConsole() {
  requestAnimationFrame(() => { el["runtime-console"].scrollTop = el["runtime-console"].scrollHeight; });
}

function formatClock(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(date);
}

function setWorkspaceMode(mode) {
  if (!["smart", "live", "review"].includes(mode)) return;
  state.mode = mode;
  const url = new URL(location.href);
  if (mode === "smart") url.searchParams.delete("mode");
  else url.searchParams.set("mode", mode);
  history.replaceState({}, "", url);
  renderWorkspaceMode();
}

function renderWorkspaceMode() {
  const activeRuntime = runtimeTasksForDisplay().some((task) => ["running", "starting"].includes(task.status));
  const previousResolvedMode = state.resolvedMode;
  state.resolvedMode = state.mode === "smart" ? (activeRuntime ? "live" : "review") : state.mode;
  document.body.classList.toggle("mode-live", state.resolvedMode === "live");
  document.body.classList.toggle("mode-review", state.resolvedMode === "review");
  el["mode-switcher"].querySelectorAll("button[data-mode]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.mode === state.mode));
  });
  el["runtime-dashboard"].classList.toggle("runtime-quiet", state.resolvedMode === "review" && !activeRuntime);
  if ((activeRuntime && !state.runtimeWasActive) || (state.resolvedMode === "live" && previousResolvedMode !== "live")) {
    el["runtime-shell"].open = true;
  }
  state.runtimeWasActive = activeRuntime;
  if (state.resolvedMode === "live") {
    el["daily-readiness"].after(el["runtime-shell"]);
  } else {
    el["metric-rail"].after(el["runtime-shell"]);
  }

  const data = state.data;
  if (!data) return;
  const viewLabel = state.viewDate === todayISO() ? "今日" : state.viewDate;
  if (isFallbackView(data)) {
    el["data-context-title"].textContent = `${viewLabel}状态 · 复盘参考 ${data.review_date}`;
    el["data-context-detail"].textContent = `实时任务属于当前时刻；交易指标和证据来自最近交易日 ${data.review_date}`;
  } else {
    el["data-context-title"].textContent = `${viewLabel} · 当日交易复盘`;
    el["data-context-detail"].textContent = `指标、订单和证据均对应 ${data.review_date}`;
  }
}

function renderFreshness() {
  const data = state.data || {};
  const selectedTask = selectedRuntimeTask();
  const activeTask = state.runtimeTasks.find((task) => task.status === "running") || null;
  const sources = Array.isArray(data.sources) ? data.sources : [];
  const missingSources = sources.filter((source) => source.status === "missing").length;
  const explainedSources = sources.filter((source) => source.status === "manual_assumed").length;
  const healthySources = sources.filter((source) => ["healthy", "present", "manual_assumed"].includes(source.status)).length;
  setFreshnessItem("freshness-page", state.runtimePayload?.generated_at || data.generated_at, "success", "刚刚刷新");

  if (activeTask) {
    const age = finiteAge(activeTask.heartbeat_at);
    setFreshnessItem("freshness-runtime", activeTask.heartbeat_at, age <= 5 ? "success" : age <= 15 ? "warning" : "critical", `PID ${activeTask.pid}`);
  } else {
    setFreshnessItem("freshness-runtime", null, "neutral", selectedTask ? "当前已停止" : "未发现任务");
  }

  const broker = data.broker || {};
  if (broker.status === "verified") {
    const age = finiteAge(broker.synced_at);
    setFreshnessItem("freshness-broker", broker.synced_at, age <= 120 ? "success" : age <= 600 ? "warning" : "critical", "只读已核对");
  } else {
    setFreshnessItem("freshness-broker", null, broker.status === "unavailable" ? "critical" : "warning", broker.error || "尚未核对");
  }

  const sourceTone = missingSources ? "critical" : healthySources ? "success" : "warning";
  const sourceText = isFallbackView(data) ? `参考日文件 ${healthySources}/${sources.length}` : `正常 ${healthySources}/${sources.length}`;
  setFreshnessItem("freshness-source", null, sourceTone, missingSources ? `${sourceText} · 缺 ${missingSources}` : sourceText);
  if (explainedSources && !missingSources) el["freshness-source"].title = `${explainedSources} 个策略账本缺口已按手动交易解释`;
  setFreshnessItem("freshness-environment", null, broker.mode === "live" ? "warning" : broker.mode ? "success" : "neutral", broker.mode ? String(broker.mode).toUpperCase() : "—");
}

function setFreshnessItem(id, timestamp, tone, fallback) {
  const value = el[id];
  const item = value.closest(".freshness-item");
  item.className = `freshness-item ${tone}`;
  value.textContent = timestamp ? formatAge(timestamp) : fallback;
  if (timestamp && fallback) value.title = `${fallback} · ${formatDateTime(timestamp)}`;
  else value.removeAttribute("title");
}

function finiteAge(value) {
  if (!value) return Number.POSITIVE_INFINITY;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? Number.POSITIVE_INFINITY : Math.max(0, (Date.now() - date.getTime()) / 1000);
}

function formatAge(value) {
  const seconds = finiteAge(value);
  if (!Number.isFinite(seconds)) return "时间未知";
  if (seconds < 5) return "刚刚";
  if (seconds < 60) return `${Math.floor(seconds)} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
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
    query.set("date", local.requested_date || local.review_date);
    query.set("broker", "1");
    try {
      const reconciled = await fetchJSON(`${API.review}?${query}`);
      if (token !== state.requestToken) return;
      state.data = reconciled;
      syncDateState();
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
  renderWorkspaceMode();
  renderFreshness();
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
  const fallback = isFallbackView(data);
  const requestedTradingDay = data.market_day?.requested_is_trading_day !== false;
  const hasRecords = data.market_day?.has_records !== false;
  const viewingToday = state.viewDate === todayISO();
  el["review-date"].value = state.viewDate || data.review_date || "";
  const complete = (data.summary?.rounds?.intraday || 0) > 0;
  const reviewLabel = !requestedTradingDay
    ? (viewingToday ? "今日休市" : "所选日休市")
    : (!hasRecords ? (viewingToday ? "今日无记录" : "所选日无记录") : (complete ? "复盘已完成" : "数据不完整"));
  el["review-status"].className = `status-indicator ${complete ? "success" : "warning"}`;
  el["review-status"].innerHTML = `<span class="status-dot" aria-hidden="true"></span><span>${reviewLabel}</span>`;
  const phaseRanges = data.phases || [];
  const first = phaseRanges.find((item) => item.start_at)?.start_at;
  const last = [...phaseRanges].reverse().find((item) => item.end_at)?.end_at;
  const coverageValue = first && last ? `${formatTime(first)}—${formatTime(last)} ET` : "覆盖时间不完整";
  const coverage = fallback ? `参考日 ${coverageValue}` : coverageValue;
  el["coverage-status"].innerHTML = `${icon("clock")}<span>${coverage}</span>`;
  const brokerStatus = state.brokerLoading ? "loading" : data.quality?.broker_status;
  const brokerMap = {
    loading: ["warning", "正在只读核对…"],
    verified: ["success", `Alpaca ${String(data.broker?.mode || "").toUpperCase()} ${fallback ? "参考日已核对" : "已核对"}`],
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
  el["today-button"].disabled = viewingToday;
  updateDateButtons();
}

function renderHeadline() {
  const data = state.data || {};
  if (data.market_day?.has_records === false || data.market_day?.requested_is_trading_day === false) {
    const viewingToday = state.viewDate === todayISO();
    const requestedTradingDay = data.market_day?.requested_is_trading_day !== false;
    const dateLabel = viewingToday ? "今日" : state.viewDate;
    el["headline-title"].textContent = requestedTradingDay
      ? `${dateLabel}暂无复盘记录`
      : `${dateLabel}休市`;
    el["headline-detail"].textContent = "页面只展示所选日期的数据，不会引用或混入其他日期的交易记录。";
    return;
  }
  const headline = state.data?.headline || {};
  el["headline-title"].textContent = headline.title || "当天复盘";
  el["headline-detail"].textContent = headline.detail || "暂无补充结论。";
}

function renderConflict() {
  const critical = (state.data?.attention || []).find((item) => item.severity === "critical");
  el["conflict-banner"].hidden = !critical;
  if (!critical) return;
  const prefix = isFallbackView(state.data) ? "最近交易日参考 · " : "必须核对 · ";
  el["conflict-title"].textContent = `${prefix}${critical.title}`;
  el["conflict-detail"].textContent = critical.message;
}

function metricDefinitions() {
  const s = state.data?.summary || {};
  const referenceDate = isFallbackView(state.data) ? state.data.review_date : "";
  const label = (currentLabel, referenceLabel) => referenceDate ? referenceLabel : currentLabel;
  const subtitle = (value) => referenceDate ? `${value} · ${referenceDate}` : value;
  return [
    { label: label("买入", "参考日买入"), value: num(s.broker_bought_symbols), subtitle: subtitle("有成交股票"), icon: "bag", tone: "neutral", bucket: "broker_filled" },
    { label: label("已卖清", "参考日已卖清"), value: num(s.broker_closed_symbols), subtitle: subtitle("完成买卖闭环"), icon: "check", tone: "neutral", bucket: "broker_closed" },
    { label: label("未成交", "参考日未成交"), value: num(s.broker_unfilled_buy_symbols), subtitle: subtitle("需要确认或重评"), icon: "hourglass", tone: "warning", bucket: "buy_unfilled" },
    { label: label("待买观察", "参考日观察池"), value: num(s.watch_counts?.intraday), subtitle: subtitle("盘中 MA5 候选"), icon: "users", tone: "neutral", bucket: "strategy" },
    { label: "当前持仓", value: num(s.current_positions), subtitle: "券商当前快照", icon: "wallet", tone: "neutral", bucket: "current_position" },
  ];
}

function renderMetricSkeletons() {
  el["metric-rail"].innerHTML = Array.from({ length: 5 }, () => `<div class="metric-skeleton skeleton"></div>`).join("");
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
  const excludedLabel = isFallbackView(state.data) ? "参考日排除" : "今日排除";
  const definitions = [
    ["", "全部", symbols.length],
    ["broker_filled", "券商已成交", symbols.filter((item) => ["broker_closed", "broker_bought"].includes(item.bucket)).length],
    ["buy_unfilled", "买入未成", symbols.filter((item) => item.bucket === "buy_unfilled").length],
    ["strategy", "策略未买", symbols.filter((item) => ["not_bought", "window_outside_closest", "excluded"].includes(item.bucket)).length],
    ["excluded", excludedLabel, symbols.filter((item) => item.bucket === "excluded").length],
    ["manual_activity", "疑似手动", symbols.filter((item) => item.local_ledger_match === "missing").length],
    ["data_conflict", "数据冲突", symbols.filter((item) => ["partial", "unmatched"].includes(item.local_ledger_match)).length],
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
    if (Number(item.sell_filled_qty) > 0 && Number(item.buy_filled_qty) <= 0) {
      return { text: `卖出回款 ${money(item.net_cash_flow)}`, className: "number-flow" };
    }
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
  const hasCritical = items.some((item) => item.severity === "critical");
  const hasWarning = items.some((item) => item.severity === "warning");
  el["attention-title"].textContent = hasCritical ? "必须核对" : "复盘提示";
  el["attention-panel"].classList.toggle("has-critical", hasCritical);
  el["attention-panel"].classList.toggle("only-info", !hasCritical && !hasWarning);
  el["attention-count"].textContent = String(items.length);
  el["attention-empty"].hidden = items.length > 0;
  el["attention-list"].hidden = items.length === 0;
  el["attention-list"].innerHTML = items.map((item) => `
    <li class="attention-item severity-${severityClass(item.severity)}">
      ${icon(item.severity === "info" ? "info" : "alert")}
      <div><strong>${escapeHTML(item.title)}</strong><p>${escapeHTML(item.message)}</p><button type="button" data-attention="${escapeAttr(item.code)}">${escapeHTML(item.action_label || "查看相关证据")}</button></div>
    </li>`).join("");
  el["attention-list"].querySelectorAll("button").forEach((button) => button.addEventListener("click", () => {
    if (["BROKER_ACTIVITY_NOT_IN_LOCAL_LEDGER", "OPEN_BUY_ORDER_WITHOUT_LOCAL_LEDGER", "UNMATCHED_POSITION_CHANGES"].includes(button.dataset.attention)) {
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
  el["reasons-title"].textContent = isFallbackView(state.data) ? `未买原因（参考日 ${state.data.review_date}）` : "未买原因（当日）";
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
  const rows = sources.map((source) => ({
    label: source.label,
    detail: source.status === "manual_assumed"
      ? "策略账本未生成 · 券商独立成交默认按手动交易处理"
      : `${source.file} · ${bytes(source.bytes)} · ${source.modified_at ? formatDateTime(source.modified_at) : "未生成"}`,
    value: source.status,
    status: source.status === "healthy" || source.status === "present" ? "success" : (source.status === "manual_assumed" ? "info" : (source.status === "empty" ? "warning" : "critical")),
  }));
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
  const manualAssumed = item.local_ledger_match === "missing";
  const checks = [
    [item.buy_filled_qty > 0, "Alpaca 买入成交已确认"],
    [(item.position_events || []).some((event) => ["added_observed", "existing_at_open"].includes(event.event_type)), "监控日志观察到持仓"],
    [item.sell_filled_qty > 0, "Alpaca 卖出成交已确认"],
    [float(item.current_position_qty) === 0, "当前券商持仓为 0"],
    [item.local_ledger_match === "matched" || manualAssumed, manualAssumed ? "策略账本无记录，默认按手动交易处理" : `本地账本${matchLabel(item.local_ledger_match).label}`],
    [manualAssumed, manualAssumed ? "手动交易不计入策略执行异常" : "下单来源/操作者/策略需要以 order_id 继续归因"],
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
  const originText = item.local_ledger_match === "missing"
    ? (membership ? "该股在 MA5 观察池内，但没有策略 order_id；按当前规则默认归类为手动交易。" : "该股不在当天 MA5 观察池且没有策略 order_id，默认归类为手动交易。")
    : (membership ? "该股在 MA5 策略观察池内；是否由策略下单仍需匹配本地 order_id。" : "该股不在当天 14 只 MA5 观察池；不能从现有证据确认是本策略下单。");
  el["drawer-strategy-context"].innerHTML = `
    <article class="snapshot-card"><h4>归因边界</h4><p class="muted">${escapeHTML(originText)}</p></article>
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
    case "manual_activity": return item.local_ledger_match === "missing";
    case "data_conflict": return ["partial", "unmatched"].includes(item.local_ledger_match);
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
  state.viewDate = validDate(data.requested_date) ? data.requested_date : data.review_date;
  el["review-date"].value = state.viewDate;
  const url = new URL(location.href);
  url.searchParams.set("date", state.viewDate);
  history.replaceState({}, "", url);
}

function updateDateButtons() {
  const dateValue = state.viewDate || state.data?.review_date;
  const index = state.dates.indexOf(dateValue);
  if (index >= 0) {
    el["previous-date"].disabled = index >= state.dates.length - 1;
    el["next-date"].disabled = index <= 0;
  } else {
    el["previous-date"].disabled = !state.dates.some((dateValueItem) => dateValueItem < dateValue);
    el["next-date"].disabled = !state.dates.some((dateValueItem) => dateValueItem > dateValue);
  }
}

function navigateDate(direction) {
  const dateValue = state.viewDate || state.data?.review_date;
  const index = state.dates.indexOf(dateValue);
  let target;
  if (index >= 0) target = direction === "previous" ? state.dates[index + 1] : state.dates[index - 1];
  else if (direction === "previous") target = state.dates.find((dateValueItem) => dateValueItem < dateValue);
  else target = [...state.dates].reverse().find((dateValueItem) => dateValueItem > dateValue);
  if (target) loadReview(target);
}

function setBusy(busy) {
  el["refresh-button"].disabled = busy;
  el["refresh-button"].classList.toggle("is-loading", busy);
  el["review-date"].disabled = busy;
  el["today-button"].disabled = busy || state.viewDate === todayISO();
  if (busy) announce("正在加载当天复盘");
}

function showError(message) {
  el["page-error"].hidden = false;
  el["page-error-message"].textContent = message;
  el["metric-rail"].setAttribute("aria-busy", "false");
}
function hideError() { el["page-error"].hidden = true; }

async function fetchJSON(url, options = {}) {
  const { timeoutMs = 45000, headers: suppliedHeaders = {}, signal: upstreamSignal, ...requestOptions } = options;
  const controller = new AbortController();
  const abortFromUpstream = () => controller.abort(upstreamSignal?.reason);
  if (upstreamSignal) upstreamSignal.addEventListener("abort", abortFromUpstream, { once: true });
  const timeoutId = window.setTimeout(() => controller.abort(), Math.max(1, Number(timeoutMs) || 45000));
  try {
    const headers = { Accept: "application/json", ...suppliedHeaders };
    const response = await fetch(url, { ...requestOptions, headers, cache: "no-store", signal: controller.signal });
    let payload;
    try { payload = await response.json(); } catch { payload = null; }
    if (!response.ok) throw new Error(payload?.error || `HTTP ${response.status}`);
    return payload;
  } catch (error) {
    if (error?.name === "AbortError") throw new Error("请求超时，请稍后重试");
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
    if (upstreamSignal) upstreamSignal.removeEventListener("abort", abortFromUpstream);
  }
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
    missing: { label: "疑似手动", className: "number-info", icon: "info" },
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
function healthStatus(value) { return ({ healthy: "正常", present: "正常", empty: "空文件", missing: "缺失", manual_assumed: "按手动交易", verified: "已核对", unavailable: "不可用", not_requested: "未核对" })[value] || String(value || "未知"); }
function bucketPriority(value) { return ({ broker_closed: 0, broker_bought: 1, buy_unfilled: 2, position_unreconciled: 3, excluded: 4, window_outside_closest: 5, not_bought: 6, broker_activity: 7 })[value] ?? 99; }
function bucketLabel(value) { return ({ broker_filled: "券商已成交", broker_closed: "已全部卖出", broker_activity: "券商订单", buy_unfilled: "买入未成", strategy: "策略未买", excluded: isFallbackView(state.data) ? "参考日排除" : "今日排除", current_position: "当前持仓", cash_flow: "有成交净现金流", manual_activity: "疑似手动", data_conflict: "数据冲突" })[value] || "全部"; }
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
function isFallbackView(data) { return Boolean(data?.market_day?.is_fallback && data.requested_date && data.requested_date !== data.review_date); }
function todayISO() {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date());
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}
function addDaysISO(value, days) {
  const [year, month, day] = String(value).split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day + days));
  return date.toISOString().slice(0, 10);
}
function isoWeekday(value) {
  const [year, month, day] = String(value).split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day)).getUTCDay();
}
function formatShortDate(value, includeWeekday = false) {
  const [year, month, day] = String(value).split("-").map(Number);
  if (![year, month, day].every(Number.isFinite)) return String(value || "—");
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "UTC",
    month: "numeric",
    day: "numeric",
    ...(includeWeekday ? { weekday: "short" } : {}),
  }).format(new Date(Date.UTC(year, month - 1, day)));
}
function localeCompare(a, b) { return a.localeCompare(b, "zh-CN", { numeric: true, sensitivity: "base" }); }
function emptyText(text) { return `<div class="section-empty compact-empty">${icon("info")}<span>${escapeHTML(text)}</span></div>`; }
function isTextInput(target) { return target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement || target?.isContentEditable; }
function icon(name) { return `<svg aria-hidden="true"><use href="#icon-${escapeAttr(name)}"></use></svg>`; }
function escapeHTML(value) { return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]); }
function escapeAttr(value) { return escapeHTML(value).replace(/`/g, "&#96;"); }
