from __future__ import annotations

import html
import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from .config import Settings
from .watchlist import normalize_symbol, read_watch_codes


CHART_FILE = "watch_code_daily_kline_latest.html"
CHART_DAYS = 30


def write_watchlist_chart_page(settings: Settings, candidates, bars_by_symbol: dict[str, list], days: int = CHART_DAYS) -> Path:
    """用 watch_codes.txt 作为唯一基准，写出 StockAPI 同款观察池 K 线页面。"""
    report_date = chart_report_date(settings, candidates)
    chart_items: list[dict[str, Any]] = []
    errors: list[str] = []
    candidate_by_code = {normalize_symbol(candidate.symbol): candidate for candidate in candidates}

    for code in read_watch_codes(settings.watch_codes_file):
        candidate = candidate_by_code.get(code)
        source_symbol = candidate.symbol if candidate else code
        try:
            bars = prepare_chart_bars(candidate_bars(source_symbol, code, bars_by_symbol), days=days)
            signal_date = candidate.signal_date if candidate else report_date
            chart_items.append({"code": code, "sort_return": chart_sort_return(bars), "html": render_daily_kline_card(code, bars, signal_date, days=days)})
        except Exception as exc:
            message = f"{code}: {type(exc).__name__}: {exc}"
            errors.append(message)
            chart_items.append({"code": code, "sort_return": float("inf"), "html": render_error_card(code, message)})

    sorted_codes = [str(item["code"]) for item in chart_items]
    chart_cards = [str(item["html"]) for item in chart_items]
    return write_watch_code_daily_kline_chart_page(settings, chart_cards, sorted_codes, errors, report_date, days)


def chart_report_date(settings: Settings, candidates) -> date:
    """优先使用 watch_codes.txt 里的 signal_date 注释，和当前观察池文件保持一致。"""
    signal_date = watch_codes_signal_date(settings.watch_codes_file)
    if signal_date:
        return signal_date
    return candidates[0].signal_date if candidates else date.today()


def watch_codes_signal_date(path: Path) -> date | None:
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        value = line.strip()
        if not value.startswith("# signal_date="):
            continue
        raw_date = value.split("=", 1)[1].strip()
        try:
            return date.fromisoformat(raw_date)
        except ValueError:
            return None
    return None


def candidate_bars(symbol: str, code: str, bars_by_symbol: dict[str, list]) -> list:
    """兼容 Alpaca 的 DEMO key 和页面使用的 US.DEMO code。"""
    plain = code[3:] if code.startswith("US.") else code
    return bars_by_symbol.get(symbol.upper()) or bars_by_symbol.get(plain.upper()) or bars_by_symbol.get(code.upper()) or []


def prepare_chart_bars(raw_bars: list, *, days: int = CHART_DAYS) -> pd.DataFrame:
    """把 Alpaca 日线列表整理成 StockAPI 图表需要的 DataFrame。"""
    if not raw_bars:
        return pd.DataFrame(columns=["date", "time_key", "open", "high", "low", "close", "volume", "prev_close", "ma5", "ma10", "ma20"])

    rows = []
    for bar in sorted(raw_bars, key=lambda item: item.date):
        rows.append(
            {
                "date": bar.date,
                "time_key": bar.date.isoformat(),
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(getattr(bar, "volume", 0) or 0),
            }
        )

    bars = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    bars["prev_close"] = bars["close"].shift(1)
    for window in (5, 10, 20):
        bars[f"ma{window}"] = bars["close"].rolling(window).mean()
    return bars.tail(days).reset_index(drop=True)


def chart_sort_return(bars: pd.DataFrame, lookback_days: int = 7) -> float:
    """页面按近几日区间涨幅排序，和 StockAPI 的展示顺序保持一致。"""
    if bars is None or bars.empty:
        return float("inf")
    window = bars.tail(max(1, lookback_days))
    if window.empty:
        return float("inf")
    first = window.iloc[0]
    last = window.iloc[-1]
    start_price = pd.to_numeric(first.get("prev_close"), errors="coerce")
    if pd.isna(start_price) or float(start_price) <= 0:
        start_price = pd.to_numeric(first.get("open"), errors="coerce")
    end_price = pd.to_numeric(last.get("close"), errors="coerce")
    if pd.isna(start_price) or pd.isna(end_price) or float(start_price) <= 0:
        return float("inf")
    return float(end_price) / float(start_price) - 1


def write_watch_code_daily_kline_chart_page(
    settings: Settings,
    chart_cards: list[str],
    codes: list[str],
    errors: list[str],
    report_date: date,
    days: int,
) -> Path:
    """写 dated HTML，并复制一份 latest 给手机固定 URL 访问。"""
    output_dir = settings.output_dir / "watchlist_charts"
    output_dir.mkdir(parents=True, exist_ok=True)
    page = output_dir / f"watch_code_daily_kline_{report_date.isoformat()}.html"
    latest = output_dir / CHART_FILE
    page.write_text(
        render_watch_code_daily_kline_page(chart_cards, codes, errors, report_date, days, int(settings.watchlist_chart_lan_port)),
        encoding="utf-8",
    )
    shutil.copyfile(page, latest)
    return page


def ensure_watchlist_chart_server_running(settings: Settings) -> None:
    """启动本地图表服务；如果端口已有旧服务，只提示不打断生成。"""
    port = int(settings.watchlist_chart_lan_port)
    if watchlist_chart_server_ready(port):
        return
    if tcp_port_is_open("127.0.0.1", port):
        print(f"Watchlist chart port {port} is open, but delete API health check failed. Restart the chart server if delete does not work.", flush=True)
        return

    script = Path(__file__).resolve().parent.parent / "tools" / "serve_watchlist_charts_lan.py"
    log_dir = settings.output_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout = (log_dir / "watchlist_chart_server.out.log").open("a", encoding="utf-8")
    stderr = (log_dir / "watchlist_chart_server.err.log").open("a", encoding="utf-8")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [sys.executable, str(script)],
        cwd=str(Path(__file__).resolve().parent.parent),
        stdout=stdout,
        stderr=stderr,
        creationflags=creationflags,
    )
    if not wait_for_tcp_port("127.0.0.1", port, timeout_seconds=8.0):
        raise RuntimeError(f"watchlist chart server did not start on port {port}")


def watchlist_chart_http_url(settings: Settings) -> str:
    """返回手机或浏览器直接打开的 latest HTML URL。"""
    host = settings.watchlist_chart_lan_host or first_lan_ip() or "127.0.0.1"
    return f"http://{host}:{int(settings.watchlist_chart_lan_port)}/{CHART_FILE}"


def watchlist_chart_server_ready(port: int) -> bool:
    """确认端口上运行的是新版图表服务，而不只是普通静态服务。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/watchlist/health", timeout=1.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return bool(payload.get("ok") and payload.get("service") == "watchlist_chart")
    except Exception:
        return False


def render_watch_code_daily_kline_page(
    chart_cards: list[str],
    codes: list[str],
    errors: list[str],
    report_date: date,
    days: int,
    server_port: int,
) -> str:
    code_text = ", ".join(html.escape(code) for code in codes) if codes else "无"
    options = "\n".join(f'<option value="{html.escape(code)}">{html.escape(code)}</option>' for code in codes)
    datalist_options = "\n".join(f'<option value="{html.escape(code)}"></option>' for code in codes)
    sidebar_items = "\n".join(render_sidebar_stock_button(code) for code in codes)
    controls = ""
    if codes:
        controls = f"""<div class="controls">
      <div class="control-group primary-controls">
        <button type="button" id="prevStock">上一只</button>
        <input id="stockSearch" list="stockDatalist" placeholder="搜索股票代码" aria-label="搜索股票代码">
        <datalist id="stockDatalist">
        {datalist_options}
        </datalist>
        <select id="stockSelect" aria-label="选择股票">
        {options}
        </select>
        <button type="button" id="nextStock">下一只</button>
        <button type="button" id="showAllStocks">全部</button>
        <button type="button" id="selectAllStocks">全选</button>
        <button type="button" id="clearSelectedStocks">清空</button>
        <button type="button" id="deleteSelectedStocks" class="danger-action">删除选中</button>
        <span id="deleteStatus" class="delete-status"></span>
      </div>
      <div class="control-group ma-controls">
        <label><input type="checkbox" class="maToggle" data-ma="ma5" checked> MA5</label>
        <label><input type="checkbox" class="maToggle" data-ma="ma10" checked> MA10</label>
        <label><input type="checkbox" class="maToggle" data-ma="ma20" checked> MA20</label>
      </div>
    </div>"""
    error_html = ""
    if errors:
        error_items = "".join(f"<li>{html.escape(error)}</li>" for error in errors)
        error_html = f'<section class="errors"><h2>加载失败</h2><ul>{error_items}</ul></section>'
    cards = "\n".join(chart_cards) if chart_cards else '<section class="empty">没有可展示的观察股票。</section>'
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>观察池近 {days} 日 K 线</title>
  <style>
    :root {{ color-scheme: light; --ink:#172033; --muted:#667085; --line:#d8e0ee; --panel:#ffffff; --bg:#f3f6fb; --soft:#eef3fb; --accent:#1f6feb; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family: Arial, "Microsoft YaHei", sans-serif; background:var(--bg); color:var(--ink); }}
    header {{ position:sticky; top:0; z-index:4; padding:14px 22px; background:#fffffff2; border-bottom:1px solid var(--line); backdrop-filter: blur(10px); }}
    h1 {{ margin:0 0 4px; font-size:22px; letter-spacing:0; }}
    .meta {{ color:var(--muted); font-size:13px; line-height:1.45; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
    .controls {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-top:12px; }}
    .control-group {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; }}
    .primary-controls {{ flex:1; min-width:520px; }}
    .ma-controls {{ margin-left:auto; }}
    select, input[type="text"], input[list] {{ min-width:210px; height:38px; padding:0 11px; border:1px solid #b7c4d8; border-radius:6px; background:#fff; color:var(--ink); font-size:14px; }}
    label {{ color:var(--muted); font-size:13px; user-select:none; }}
    button {{ height:38px; padding:0 13px; border:1px solid #b7c4d8; border-radius:6px; background:#fff; color:var(--ink); cursor:pointer; font-size:14px; }}
    button:hover {{ background:#eef4ff; }}
    .delete-watch-code {{ height:32px; border-color:#fecaca; color:#b91c1c; }}
    .delete-watch-code:hover {{ background:#fff1f2; }}
    .danger-action {{ border-color:#fecaca; color:#b91c1c; }}
    .danger-action:hover {{ background:#fff1f2; }}
    .delete-status {{ min-height:18px; color:var(--muted); font-size:13px; }}
    .delete-status.is-error {{ color:#b91c1c; }}
    .delete-status.is-ok {{ color:#166534; }}
    .app-shell {{ display:grid; grid-template-columns:260px minmax(0, 1fr); gap:16px; padding:16px 18px 32px; max-width:1720px; margin:0 auto; }}
    .sidebar {{ position:sticky; top:116px; height:calc(100vh - 136px); overflow:auto; background:#fff; border:1px solid var(--line); border-radius:8px; padding:12px; box-shadow:0 1px 2px rgba(16,24,40,.05); }}
    .sidebar-title {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; color:var(--muted); font-size:13px; }}
    .stock-list {{ display:grid; gap:6px; }}
    .stock-row {{ display:grid; grid-template-columns:26px minmax(0, 1fr); gap:4px; align-items:center; }}
    .stock-select {{ width:16px; height:16px; margin:0 auto; }}
    .stock-nav-button {{ display:flex; justify-content:space-between; align-items:center; width:100%; height:34px; padding:0 10px; border-color:transparent; background:#f8fafc; text-align:left; font-weight:700; }}
    .stock-nav-button:hover {{ background:#eef4ff; }}
    .stock-nav-button.is-active {{ background:#e8f1ff; border-color:#9dc2ff; color:#0f4fa8; }}
    .stock-index {{ color:var(--muted); font-size:12px; font-weight:400; }}
    .chart-stage {{ min-width:0; }}
    .card {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px 18px 20px; margin-bottom:18px; box-shadow:0 1px 2px rgba(16,24,40,.05); }}
    .card[hidden] {{ display:none; }}
    .card-title {{ display:flex; justify-content:space-between; gap:14px; align-items:flex-start; margin-bottom:12px; }}
    .code {{ font-size:24px; font-weight:800; line-height:1.1; }}
    .summary {{ color:var(--muted); font-size:13px; }}
    .card-actions {{ display:flex; align-items:center; gap:10px; margin-left:auto; }}
    .metric-grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(136px, 1fr)); gap:8px; margin:10px 0 12px; }}
    .metric {{ background:var(--soft); border:1px solid #e4ebf6; border-radius:7px; padding:8px 10px; }}
    .metric-label {{ color:var(--muted); font-size:12px; margin-bottom:3px; }}
    .metric-value {{ font-size:16px; font-weight:750; }}
    .metric-value.positive {{ color:#14804a; }}
    .metric-value.negative {{ color:#c24135; }}
    .legend {{ display:flex; gap:14px; flex-wrap:wrap; margin:8px 0 8px; color:var(--muted); font-size:12px; }}
    .legend span::before {{ content:""; display:inline-block; width:20px; height:3px; margin-right:6px; vertical-align:middle; border-radius:2px; background:currentColor; }}
    .ma5 {{ color:#2563eb; }} .ma10 {{ color:#f59e0b; }} .ma20 {{ color:#7c3aed; }}
    .chart-wrap {{ width:100%; min-height:560px; }}
    svg {{ width:100%; min-height:560px; height:auto; display:block; overflow:visible; }}
    .axis {{ fill:#667085; font-size:12px; }}
    .pct-label {{ font-size:11px; font-weight:700; paint-order:stroke; stroke:#fff; stroke-width:3px; stroke-linejoin:round; }}
    .grid {{ stroke:#e6edf7; stroke-width:1; }}
    .hover-target {{ fill:transparent; cursor:crosshair; pointer-events:all; }}
    .hover-target:hover + .hover-marker {{ opacity:1; }}
    .hover-marker {{ opacity:0; pointer-events:none; }}
    .ma-line.is-hidden {{ display:none; }}
    #chartTooltip {{ position:fixed; z-index:10; display:none; max-width:310px; padding:10px 12px; background:#111827; color:#fff; border-radius:7px; font-size:12px; line-height:1.55; box-shadow:0 8px 24px rgba(15,23,42,.22); white-space:pre-line; }}
    .errors, .empty {{ background:#fff7ed; border:1px solid #fed7aa; border-radius:8px; padding:12px 16px; margin-bottom:16px; }}
    .error-card {{ border-color:#fecaca; background:#fffafa; }}
    @media (max-width: 980px) {{
      .primary-controls {{ min-width:100%; }}
      .app-shell {{ grid-template-columns:1fr; padding:12px; }}
      .sidebar {{ position:static; height:auto; max-height:220px; }}
      .metric-grid {{ grid-template-columns:repeat(2, minmax(0, 1fr)); }}
      svg, .chart-wrap {{ min-height:420px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>观察池近 {days} 日 K 线 + MA5/MA10/MA20</h1>
    <div class="meta">生成日期：{html.escape(report_date.isoformat())} | 股票数量：{len(codes)} | 股票：{code_text}</div>
    {controls}
  </header>
  <main class="app-shell">
    <aside class="sidebar">
      <div class="sidebar-title"><span>观察股票</span><span>{len(codes)}</span></div>
      <div class="stock-list" id="stockListButtons">
        {sidebar_items}
      </div>
    </aside>
    <section class="chart-stage">
      {error_html}
      {cards}
    </section>
  </main>
  <script>
    const select = document.getElementById('stockSelect');
    const search = document.getElementById('stockSearch');
    function stockCards() {{
      return Array.from(document.querySelectorAll('[data-stock-card]'));
    }}
    const prev = document.getElementById('prevStock');
    const next = document.getElementById('nextStock');
    const showAllButton = document.getElementById('showAllStocks');
    const deleteSelectedButton = document.getElementById('deleteSelectedStocks');
    const selectAllButton = document.getElementById('selectAllStocks');
    const clearSelectedButton = document.getElementById('clearSelectedStocks');
    const deleteStatus = document.getElementById('deleteStatus');
    const tooltip = document.createElement('div');
    tooltip.id = 'chartTooltip';
    document.body.appendChild(tooltip);

    function showStock(code) {{
      stockCards().forEach(card => {{
        card.hidden = card.dataset.code !== code;
      }});
      document.querySelectorAll('[data-stock-nav]').forEach(button => {{
        button.classList.toggle('is-active', button.dataset.stockNav === code);
      }});
      if (select && select.value !== code) select.value = code;
      if (search && search.value !== code) search.value = code;
    }}

    function moveStock(offset) {{
      if (!select || select.options.length === 0) return;
      const nextIndex = (select.selectedIndex + offset + select.options.length) % select.options.length;
      select.selectedIndex = nextIndex;
      showStock(select.value);
    }}

    function showAllStocks() {{
      stockCards().forEach(card => {{
        card.hidden = false;
      }});
      document.querySelectorAll('[data-stock-nav]').forEach(button => button.classList.remove('is-active'));
    }}

    function removeSelectOption(code) {{
      if (!select) return;
      Array.from(select.options).forEach(option => {{
        if (option.value === code) option.remove();
      }});
    }}

    function removeDatalistOption(code) {{
      const datalist = document.getElementById('stockDatalist');
      if (!datalist) return;
      Array.from(datalist.options).forEach(option => {{
        if (option.value === code) option.remove();
      }});
    }}

    function selectFirstRemainingStock() {{
      if (!select || select.options.length === 0) return;
      select.selectedIndex = 0;
      showStock(select.value);
    }}

    function removeCardFromPage(code) {{
      const card = document.querySelector(`[data-stock-card][data-code="${{code}}"]`);
      if (card) card.remove();
      removeSelectOption(code);
      removeDatalistOption(code);
      const sidebarButton = document.querySelector(`[data-stock-nav="${{code}}"]`);
      const sidebarRow = document.querySelector(`[data-stock-row="${{code}}"]`);
      if (sidebarRow) sidebarRow.remove();
      else if (sidebarButton) sidebarButton.remove();
      if (search && search.value === code) search.value = '';
      selectFirstRemainingStock();
    }}

    function selectedWatchCodes() {{
      return Array.from(document.querySelectorAll('[data-select-code]:checked')).map(item => item.dataset.selectCode);
    }}

    function setDeleteStatus(message, type = '') {{
      if (!deleteStatus) return;
      deleteStatus.textContent = message || '';
      deleteStatus.classList.toggle('is-error', type === 'error');
      deleteStatus.classList.toggle('is-ok', type === 'ok');
    }}

    function updateDeleteSelectionState() {{
      const count = selectedWatchCodes().length;
      if (deleteSelectedButton) deleteSelectedButton.textContent = count > 0 ? `删除选中(${{count}})` : '删除选中';
      if (count > 0) setDeleteStatus(`已选 ${{count}} 只`);
      else setDeleteStatus('');
    }}

    function selectAllStocks() {{
      document.querySelectorAll('[data-select-code]').forEach(item => {{
        item.checked = true;
      }});
      updateDeleteSelectionState();
    }}

    function clearSelectedStocks() {{
      document.querySelectorAll('[data-select-code]').forEach(item => {{
        item.checked = false;
      }});
      updateDeleteSelectionState();
    }}

    function deleteApiUrls() {{
      const path = '/api/watchlist/delete';
      const urls = [];
      if (window.location.protocol === 'http:' || window.location.protocol === 'https:') {{
        urls.push(window.location.origin + path);
      }}
      urls.push('http://127.0.0.1:{server_port}' + path);
      urls.push('http://localhost:{server_port}' + path);
      return Array.from(new Set(urls));
    }}

    function removeCardsFromPage(codes) {{
      codes.forEach(code => removeCardFromPage(code));
      updateDeleteSelectionState();
    }}

    async function deleteWatchCode(code) {{
      if (!code) return;
      await deleteWatchCodes([code]);
    }}

    async function deleteSelectedWatchCodes() {{
      const codes = selectedWatchCodes();
      if (codes.length === 0) {{
        setDeleteStatus('请先勾选要删除的股票', 'error');
        return;
      }}
      await deleteWatchCodes(codes, false);
    }}

    async function deleteWatchCodes(codes, requireConfirm = true) {{
      if (!codes || codes.length === 0) return;
      if (requireConfirm && !window.confirm(`确认从观察池删除 ${{codes.length}} 只股票吗？\n${{codes.join(', ')}}`)) return;
      setDeleteStatus(`正在删除 ${{codes.length}} 只...`);
      const failures = [];
      for (const url of deleteApiUrls()) {{
        try {{
          const response = await fetch(url, {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ codes }}),
          }});
          const text = await response.text();
          let result;
          try {{
            result = JSON.parse(text);
          }} catch (parseError) {{
            const preview = text.slice(0, 80).replace(/\\s+/g, ' ');
            failures.push(`${{url}} 返回的不是 JSON: ${{preview}}`);
            continue;
          }}
          if (!response.ok || !result.ok) {{
            failures.push(result.error || `${{url}} HTTP ${{response.status}}`);
            continue;
          }}
          if (!result.removed) {{
            setDeleteStatus('没有在本地观察池文件中找到这些股票', 'error');
            return;
          }}
          removeCardsFromPage(result.codes || codes);
          setDeleteStatus(`已删除 ${{(result.codes || codes).length}} 只`, 'ok');
          return;
        }} catch (error) {{
          failures.push(`${{url}}: ${{error.message || error}}`);
        }}
      }}
      setDeleteStatus(`删除请求失败：${{failures.join(' | ')}}；请确认图表服务窗口仍在运行`, 'error');
    }}

    document.addEventListener('click', event => {{
      const selectedDelete = event.target.closest('#deleteSelectedStocks');
      if (selectedDelete) {{
        event.preventDefault();
        deleteSelectedWatchCodes();
        return;
      }}
      const deleteButton = event.target.closest('[data-delete-code]');
      if (deleteButton) {{
        event.preventDefault();
        deleteWatchCode(deleteButton.dataset.deleteCode);
        return;
      }}
      const navButton = event.target.closest('[data-stock-nav]');
      if (navButton) {{
        event.preventDefault();
        showStock(navButton.dataset.stockNav);
      }}
    }});

    document.addEventListener('change', event => {{
      if (event.target.closest('[data-select-code]')) updateDeleteSelectionState();
    }});

    function searchStock() {{
      if (!select || !search) return;
      const query = search.value.trim().toUpperCase();
      if (!query) return;
      const option = Array.from(select.options).find(item => item.value.toUpperCase().includes(query));
      if (!option) return;
      select.value = option.value;
      showStock(select.value);
    }}

    function setMaVisibility(ma, visible) {{
      document.querySelectorAll(`[data-ma="${{ma}}"]`).forEach(line => {{
        line.classList.toggle('is-hidden', !visible);
      }});
    }}

    function showTooltip(event, target) {{
      tooltip.textContent = target.dataset.tooltip || '';
      tooltip.style.display = 'block';
      const margin = 14;
      const nextX = Math.min(event.clientX + margin, window.innerWidth - tooltip.offsetWidth - margin);
      const nextY = Math.min(event.clientY + margin, window.innerHeight - tooltip.offsetHeight - margin);
      tooltip.style.left = `${{Math.max(margin, nextX)}}px`;
      tooltip.style.top = `${{Math.max(margin, nextY)}}px`;
    }}

    document.addEventListener('mousemove', event => {{
      const target = event.target.closest('[data-tooltip]');
      if (!target) {{
        tooltip.style.display = 'none';
        return;
      }}
      showTooltip(event, target);
    }});

    document.addEventListener('keydown', event => {{
      if (event.key === 'ArrowLeft') moveStock(-1);
      if (event.key === 'ArrowRight') moveStock(1);
    }});

    if (select && select.options.length > 0) {{
      select.addEventListener('change', () => showStock(select.value));
      if (search) {{
        search.addEventListener('change', searchStock);
        search.addEventListener('keydown', event => {{
          if (event.key === 'Enter') searchStock();
        }});
      }}
      if (prev) prev.addEventListener('click', () => moveStock(-1));
      if (next) next.addEventListener('click', () => moveStock(1));
      if (showAllButton) showAllButton.addEventListener('click', showAllStocks);
      if (selectAllButton) selectAllButton.addEventListener('click', selectAllStocks);
      if (clearSelectedButton) clearSelectedButton.addEventListener('click', clearSelectedStocks);
      document.querySelectorAll('.maToggle').forEach(toggle => {{
        toggle.addEventListener('change', () => setMaVisibility(toggle.dataset.ma, toggle.checked));
      }});
      showStock(select.value);
      updateDeleteSelectionState();
    }}
  </script>
</body>
</html>
"""


def render_daily_kline_card(code: str, bars: pd.DataFrame, signal_date: date, *, days: int = CHART_DAYS) -> str:
    safe_code = html.escape(code)
    if bars is None or bars.empty:
        return render_error_card(code, f"{code}: 没有近 {days} 日 K 线数据")
    title = chart_summary_text(bars)
    metrics = chart_metric_tiles(bars, signal_date)
    svg = render_daily_kline_svg(bars)
    return f"""<section class="card" data-stock-card data-code="{safe_code}">
  <div class="card-title"><div class="code">{safe_code}</div><div class="card-actions"><div class="summary">{html.escape(title)}</div><button type="button" class="delete-watch-code" data-delete-code="{safe_code}">删除</button></div></div>
  {metrics}
  <div class="legend"><span class="ma5">MA5</span><span class="ma10">MA10</span><span class="ma20">MA20</span></div>
  <div class="chart-wrap">{svg}</div>
</section>"""


def render_error_card(code: str, message: str) -> str:
    safe_code = html.escape(code)
    return f"""<section class="card error-card" data-stock-card data-code="{safe_code}">
  <div class="card-title"><div class="code">{safe_code}</div><div class="card-actions"><button type="button" class="delete-watch-code" data-delete-code="{safe_code}">删除</button></div></div>
  <div class="summary">{html.escape(message)}</div>
</section>"""


def render_sidebar_stock_button(code: str) -> str:
    safe_code = html.escape(code)
    short_code = safe_code.split(".")[-1]
    return f"""<div class="stock-row" data-stock-row="{safe_code}">
  <input type="checkbox" class="stock-select" data-select-code="{safe_code}" aria-label="选择 {safe_code}">
  <button type="button" class="stock-nav-button" data-stock-nav="{safe_code}">
    <span>{short_code}</span><span class="stock-index">{safe_code}</span>
  </button>
</div>"""


def chart_summary_text(bars: pd.DataFrame) -> str:
    last = bars.iloc[-1]
    first = bars.iloc[0]
    close = float(last["close"])
    change = close / float(first["close"]) - 1 if float(first["close"]) > 0 else 0.0
    return f"{first['time_key']} - {last['time_key']} | 收盘 {format_price(close)} | 区间涨跌 {change:.2%}"


def chart_metric_tiles(bars: pd.DataFrame, signal_date: date) -> str:
    last = bars.iloc[-1]
    first = bars.iloc[0]
    close = float(last["close"])
    prev_close = pd.to_numeric(last.get("prev_close"), errors="coerce")
    day_change = close / float(prev_close) - 1 if not pd.isna(prev_close) and float(prev_close) > 0 else None
    interval_change = close / float(first["close"]) - 1 if float(first["close"]) > 0 else None
    signal_close_ma5_distance = signal_day_close_ma5_distance_pct(bars, signal_date)
    interval_high = pd.to_numeric(bars["high"], errors="coerce").max()
    interval_low = pd.to_numeric(bars["low"], errors="coerce").min()
    volume = last.get("volume")
    tiles = [
        ("最新收盘", format_price(close), ""),
        ("当日涨跌", format_pct(day_change), pct_class(day_change)),
        ("区间涨跌", format_pct(interval_change), pct_class(interval_change)),
        ("信号日收盘距MA5", format_signed_pct(signal_close_ma5_distance), pct_class(signal_close_ma5_distance)),
        ("区间高低", f"{format_price(float(interval_high))} / {format_price(float(interval_low))}", ""),
        ("最新成交量", format_volume(volume), ""),
    ]
    items = []
    for label, value, class_name in tiles:
        value_class = f"metric-value {class_name}".strip()
        items.append(f"""<div class="metric"><div class="metric-label">{html.escape(label)}</div><div class="{value_class}">{html.escape(value)}</div></div>""")
    return f"""<div class="metric-grid">{''.join(items)}</div>"""


def signal_day_close_ma5_distance_pct(bars: pd.DataFrame, signal_date: date) -> float | None:
    signal_rows = bars[bars["time_key"] == signal_date.isoformat()] if "time_key" in bars.columns else pd.DataFrame()
    if signal_rows.empty:
        return None
    row = signal_rows.iloc[-1]
    close_price = pd.to_numeric(row.get("close"), errors="coerce")
    ma5 = pd.to_numeric(row.get("ma5"), errors="coerce")
    if pd.isna(close_price) or pd.isna(ma5) or float(ma5) <= 0:
        return None
    return float(close_price) / float(ma5) - 1.0


def render_daily_kline_svg(bars: pd.DataFrame) -> str:
    width, height = 1320, 560
    left, right, top, bottom = 74, 34, 24, 58
    plot_w, plot_h = width - left - right, height - top - bottom
    min_price, max_price = chart_price_bounds(bars)

    def x_at(index: int) -> float:
        return left + (index + 0.5) * plot_w / max(1, len(bars))

    def y_at(price: float) -> float:
        if max_price <= min_price:
            return top + plot_h / 2
        return top + (max_price - price) / (max_price - min_price) * plot_h

    grid = render_price_grid(left, top, plot_w, min_price, max_price, y_at)
    candles = render_candles(bars, x_at, y_at, plot_w, top, plot_h)
    ma_lines = "\n".join(
        [
            render_ma_polyline(bars, "ma5", "#2563eb", x_at, y_at),
            render_ma_polyline(bars, "ma10", "#f59e0b", x_at, y_at),
            render_ma_polyline(bars, "ma20", "#7c3aed", x_at, y_at),
        ]
    )
    date_labels = render_date_labels(bars, x_at, top + plot_h + 22)
    border = f'<rect x="{left}" y="{top}" width="{plot_w}" height="{plot_h}" fill="none" stroke="#cbd5e1"/>'
    return f'<svg viewBox="0 0 {width} {height}" role="img">{grid}{border}{candles}{ma_lines}{date_labels}</svg>'


def chart_price_bounds(bars: pd.DataFrame) -> tuple[float, float]:
    values = []
    for field in ("high", "low", "ma5", "ma10", "ma20"):
        if field in bars.columns:
            values.extend(pd.to_numeric(bars[field], errors="coerce").dropna().astype(float).tolist())
    if not values:
        return 0.0, 1.0
    low, high = min(values), max(values)
    padding = max((high - low) * 0.08, high * 0.01, 0.01)
    return max(0.0, low - padding), high + padding


def render_price_grid(left: int, top: int, plot_w: int, min_price: float, max_price: float, y_at) -> str:
    parts = []
    for index in range(5):
        price = min_price + (max_price - min_price) * index / 4
        y = y_at(price)
        parts.append(f'<line class="grid" x1="{left}" x2="{left + plot_w}" y1="{y:.2f}" y2="{y:.2f}"/>')
        parts.append(f'<text class="axis" x="6" y="{y + 4:.2f}">{html.escape(format_price(price))}</text>')
    return "\n".join(parts)


def render_candles(bars: pd.DataFrame, x_at, y_at, plot_w: int, top: int, plot_h: int) -> str:
    slot = plot_w / max(1, len(bars))
    candle_w = max(7, min(24, slot * 0.62))
    parts = []
    for index, row in bars.iterrows():
        open_price, close_price = float(row["open"]), float(row["close"])
        high_price, low_price = float(row["high"]), float(row["low"])
        x = x_at(index)
        color = "#16a34a" if close_price >= open_price else "#dc2626"
        y_high, y_low = y_at(high_price), y_at(low_price)
        y_open, y_close = y_at(open_price), y_at(close_price)
        rect_y = min(y_open, y_close)
        rect_h = max(1.0, abs(y_close - y_open))
        tooltip_attr = html.escape(candle_tooltip(row), quote=True)
        pct_text = html.escape(format_signed_pct(candle_day_change_pct(row)))
        label_y = max(top + 12, rect_y - 7) if close_price >= open_price else min(top + plot_h - 4, rect_y + rect_h + 15)
        parts.append('<g class="candle">')
        parts.append(f'<line x1="{x:.2f}" x2="{x:.2f}" y1="{y_high:.2f}" y2="{y_low:.2f}" stroke="{color}" stroke-width="1.8"/>')
        parts.append(f'<rect x="{x - candle_w / 2:.2f}" y="{rect_y:.2f}" width="{candle_w:.2f}" height="{rect_h:.2f}" fill="{color}" opacity="0.86"/>')
        parts.append(f'<text class="pct-label" x="{x:.2f}" y="{label_y:.2f}" text-anchor="middle" fill="{color}">{pct_text}</text>')
        parts.append(f'<rect class="hover-target" data-tooltip="{tooltip_attr}" x="{x - slot / 2:.2f}" y="{top}" width="{slot:.2f}" height="{plot_h}"/>')
        parts.append(f'<line class="hover-marker" x1="{x:.2f}" x2="{x:.2f}" y1="{top}" y2="{top + plot_h}" stroke="#334155" stroke-width="1.2" stroke-dasharray="4 4"/>')
        parts.append("</g>")
    return "\n".join(parts)


def candle_tooltip(row: pd.Series) -> str:
    open_price = float(row["open"])
    high_price = float(row["high"])
    low_price = float(row["low"])
    close_price = float(row["close"])
    prev_close = pd.to_numeric(row.get("prev_close"), errors="coerce")
    change_pct = close_price / float(prev_close) - 1 if not pd.isna(prev_close) and float(prev_close) > 0 else None
    intraday_pct = close_price / open_price - 1 if open_price > 0 else None
    amplitude_pct = (high_price - low_price) / float(prev_close) if not pd.isna(prev_close) and float(prev_close) > 0 else None
    parts = [
        str(row.get("time_key", "")),
        f"开: {format_price(open_price)}  高: {format_price(high_price)}",
        f"低: {format_price(low_price)}  收: {format_price(close_price)}",
        f"涨跌幅: {format_pct(change_pct)}  日内: {format_pct(intraday_pct)}",
        f"振幅: {format_pct(amplitude_pct)}",
        f"成交量: {format_volume(row.get('volume'))}",
        f"MA5: {format_optional_price(row.get('ma5'))}  MA10: {format_optional_price(row.get('ma10'))}  MA20: {format_optional_price(row.get('ma20'))}",
    ]
    return "\n".join(parts)


def candle_day_change_pct(row: pd.Series) -> float | None:
    close_price = pd.to_numeric(row.get("close"), errors="coerce")
    prev_close = pd.to_numeric(row.get("prev_close"), errors="coerce")
    if not pd.isna(prev_close) and not pd.isna(close_price) and float(prev_close) > 0:
        return float(close_price) / float(prev_close) - 1
    open_price = pd.to_numeric(row.get("open"), errors="coerce")
    if not pd.isna(open_price) and not pd.isna(close_price) and float(open_price) > 0:
        return float(close_price) / float(open_price) - 1
    return None


def render_ma_polyline(bars: pd.DataFrame, field: str, color: str, x_at, y_at) -> str:
    if field not in bars.columns:
        return ""
    points = []
    for index, value in enumerate(pd.to_numeric(bars[field], errors="coerce")):
        if not pd.isna(value):
            points.append(f"{x_at(index):.2f},{y_at(float(value)):.2f}")
    if len(points) < 2:
        return ""
    return f'<polyline class="ma-line {field}-line" data-ma="{field}" points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="2.6" stroke-linejoin="round" stroke-linecap="round"/>'


def render_date_labels(bars: pd.DataFrame, x_at, y: float) -> str:
    if bars.empty:
        return ""
    step = max(1, len(bars) // 6)
    indexes = sorted(set(list(range(0, len(bars), step)) + [len(bars) - 1]))
    parts = []
    for index in indexes:
        label = str(bars.iloc[index]["time_key"])[5:]
        parts.append(f'<text class="axis" x="{x_at(index):.2f}" y="{y:.2f}" text-anchor="middle">{html.escape(label)}</text>')
    return "\n".join(parts)


def delete_watch_code_from_watchlist(settings: Settings, code: str) -> dict[str, Any]:
    """删除一只观察股票，供 HTTP API 和测试共用。"""
    return delete_watch_codes_from_watchlist(settings, [code])


def delete_watch_codes_from_watchlist(settings: Settings, codes) -> dict[str, Any]:
    """从本项目唯一 watch_codes.txt 删除股票，并返回页面需要的 JSON。"""
    normalized = normalize_codes(codes or [])
    if not normalized:
        return {"ok": False, "error": "股票代码为空", "codes": []}

    path = settings.watch_codes_file
    if not path.exists():
        return {"ok": True, "codes": normalized, "removed": False, "paths": [], "remaining_codes": []}

    before = path.read_text(encoding="utf-8-sig").splitlines()
    after, removed = remove_codes_from_watchlist_lines(before, set(normalized))
    if removed:
        path.write_text("\n".join(after).rstrip() + "\n", encoding="utf-8")

    return {
        "ok": True,
        "code": normalized[0],
        "codes": normalized,
        "removed": removed,
        "paths": [str(path.resolve())] if removed else [],
        "remaining_codes": read_watch_codes(path),
    }


def normalize_codes(codes) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in codes:
        code = normalize_symbol(str(raw or "").strip())
        if code and code not in seen:
            seen.add(code)
            normalized.append(code)
    return normalized


def remove_codes_from_watchlist_lines(lines: list[str], codes: set[str]) -> tuple[list[str], bool]:
    removed = False
    kept: list[str] = []
    for line in lines:
        value = line.strip()
        line_code = value.split(",", 1)[0].strip()
        normalized = normalize_symbol(line_code) if line_code and not line_code.startswith("#") else ""
        if normalized and normalized in codes:
            removed = True
            continue
        kept.append(line)
    return kept, removed


def content_type_for_path(path: Path) -> str:
    if path.suffix.lower() == ".html":
        return "text/html; charset=utf-8"
    if path.suffix.lower() == ".json":
        return "application/json; charset=utf-8"
    return "application/octet-stream"


def tcp_port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_for_tcp_port(host: str, port: int, *, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() <= deadline:
        if tcp_port_is_open(host, port):
            return True
        time.sleep(0.2)
    return False


def first_lan_ip() -> str | None:
    ips = local_lan_ips()
    for ip in ips:
        if ip.startswith("10.0.0."):
            return ip
    return ips[0] if ips else None


def local_lan_ips() -> list[str]:
    ips: set[str] = set()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET):
            ip = item[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        return []
    return sorted(ips)


def format_price(value: float) -> str:
    return f"{value:.4f}" if abs(value) < 1 else f"{value:.2f}"


def format_optional_price(value) -> str:
    number = pd.to_numeric(value, errors="coerce")
    if pd.isna(number):
        return "--"
    return format_price(float(number))


def format_volume(value) -> str:
    number = pd.to_numeric(value, errors="coerce")
    if pd.isna(number) or float(number) < 0:
        return "--"
    volume = float(number)
    if volume >= 100_000_000:
        return f"{volume / 100_000_000:.2f}亿"
    if volume >= 10_000:
        return f"{volume / 10_000:.2f}万"
    return f"{volume:.0f}"


def pct_class(value: float | None) -> str:
    if value is None or pd.isna(value) or abs(float(value)) < 1e-12:
        return ""
    return "positive" if float(value) > 0 else "negative"


def format_pct(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "--"
    return f"{float(value):.2%}"


def format_signed_pct(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "--"
    return f"{float(value):+.2%}"
