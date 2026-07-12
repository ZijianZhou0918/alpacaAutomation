from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .alpaca_connection import build_trading_connection, load_alpaca_credentials
from .config import GAP_CONFIRMED_PULLBACK_STRATEGY_NAME, MA5_DIP_STRATEGY_NAME, Settings, build_settings
from .console_notify import send_console_notification
from .errors import short_error
from .final_strategy import OPTIMIZATION_RULES, WATCHLIST_SIGNAL_PARAMS
from .market_time import daily_request_end, stale_sip_daily_end
from . import strategy_ma5_dip
from .watchlist import read_watch_codes, to_alpaca_symbol
from .watchlist_charts import ensure_watchlist_chart_server_running, watchlist_chart_http_url, write_watchlist_chart_page


MIN_SIGNAL_GAIN_PCT = WATCHLIST_SIGNAL_PARAMS["MIN_SIGNAL_GAIN_PCT"]
MIN_SIGNAL_GAIN_OVER_MA5_GAIN_PCT = WATCHLIST_SIGNAL_PARAMS["MIN_SIGNAL_GAIN_OVER_MA5_GAIN_PCT"]
MIN_OPEN_TO_MA5_RATIO = WATCHLIST_SIGNAL_PARAMS["MIN_OPEN_TO_MA5_RATIO"]
MIN_CLOSE_TO_MA5_RATIO = WATCHLIST_SIGNAL_PARAMS["MIN_CLOSE_TO_MA5_RATIO"]
MAX_CLOSE_TO_MA5_RATIO = OPTIMIZATION_RULES["max_close_to_ma5_ratio"]
MIN_SIGNAL_CLOSE_POSITION_PCT = OPTIMIZATION_RULES["min_signal_close_position_pct"]
REQUIRE_MA5_GT_MA10_GT_MA20 = bool(OPTIMIZATION_RULES["require_ma5_gt_ma10_gt_ma20"])


@dataclass(frozen=True)
class WatchlistScreenRules:
    min_signal_gain_pct: float
    min_signal_gain_over_ma5_gain_pct: float
    min_open_to_ma5_ratio: float
    min_close_to_ma5_ratio: float
    max_close_to_ma5_ratio: float
    min_signal_close_position_pct: float
    require_ma5_gt_ma10_gt_ma20: bool


GAP_CONFIRMED_WATCHLIST_RULES = WatchlistScreenRules(
    MIN_SIGNAL_GAIN_PCT,
    MIN_SIGNAL_GAIN_OVER_MA5_GAIN_PCT,
    MIN_OPEN_TO_MA5_RATIO,
    MIN_CLOSE_TO_MA5_RATIO,
    MAX_CLOSE_TO_MA5_RATIO,
    MIN_SIGNAL_CLOSE_POSITION_PCT,
    REQUIRE_MA5_GT_MA10_GT_MA20,
)

MA5_DIP_WATCHLIST_RULES = WatchlistScreenRules(
    strategy_ma5_dip.MIN_SIGNAL_DAY_GAIN_PCT,
    0.0,
    0.0,
    1.0,
    999.0,
    0.0,
    True,
)


def ma5_dip_watchlist_rules() -> WatchlistScreenRules:
    return WatchlistScreenRules(
        strategy_ma5_dip.MIN_SIGNAL_DAY_GAIN_PCT,
        0.0,
        0.0,
        1.0,
        999.0,
        0.0,
        True,
    )


@dataclass(frozen=True)
class DailyBar:
    """选股筛选需要的最小日线字段。"""

    symbol: str
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    vwap: float | None = None
    transactions: int | None = None
    timestamp_ms: int | None = None


@dataclass(frozen=True)
class WatchCandidate:
    """满足选股规则并准备写入 watch_codes 的候选股票。"""

    symbol: str
    signal_date: date
    gain_pct: float
    upper_shadow_pct: float
    ma5: float
    ma10: float
    ma20: float
    open: float
    high: float
    close: float
    low: float = 0.0
    ma5_gain_pct: float = 0.0
    body_pct: float = 0.0


def generate_watch_codes(
    settings: Settings | None = None,
    symbols: list[str] | None = None,
    max_symbols: int | None = None,
    lookback_days: int = 60,
    batch_size: int = 100,
    feed: str = "sip",
) -> list[WatchCandidate]:
    """按当前选股策略生成 watch_codes.txt，并同步刷新图表页。"""
    settings = settings or build_settings()
    now_et = datetime.now(ZoneInfo(settings.market_timezone))
    symbol_pool = symbols or load_tradable_symbols(max_symbols=max_symbols)
    if max_symbols is not None:
        symbol_pool = symbol_pool[:max_symbols]
    rules = watchlist_screen_rules(settings.strategy_name)

    print(f"开始生成 watch_codes：symbols={len(symbol_pool)} feed={feed}", flush=True)
    bars_by_symbol = fetch_daily_bars(symbol_pool, now_et, lookback_days, batch_size, feed)
    candidates = screen_candidates(bars_by_symbol, now_et, rules=rules)
    validate_candidates(candidates, rules=rules)
    write_watch_codes(settings.watch_codes_file, candidates, rules=rules)
    write_candidate_report(settings.output_dir, candidates)
    chart_path = write_watchlist_chart_page(settings, candidates, bars_by_symbol)
    server_port = ensure_watchlist_chart_server_running(settings)
    chart_url = watchlist_chart_http_url(settings, port=server_port)
    print(f"生成完成：{len(candidates)} 个候选，已写入 {settings.watch_codes_file}", flush=True)
    print(f"Watchlist chart page: {chart_path}", flush=True)
    print(f"Watchlist chart HTTP URL: {chart_url}", flush=True)
    send_console_notification(
        "\n".join(
            [
                "【盘中 watch_codes 生成完成】",
                "结论：盘中交易观察池已更新。",
                "动作：后续盘中监控会基于该文件检测买入/卖出信号。",
                "",
                "生成结果",
                f"- 策略：{settings.strategy_name}",
                f"- 候选数量：{len(candidates)}",
                f"- 观察文件：{settings.watch_codes_file}",
                f"- 图表链接：{chart_url}",
            ]
        ),
        context="watchcode generated",
        settings=settings,
    )
    return candidates


def watchlist_screen_rules(strategy_name: str | None = None) -> WatchlistScreenRules:
    if (strategy_name or GAP_CONFIRMED_PULLBACK_STRATEGY_NAME) == MA5_DIP_STRATEGY_NAME:
        return ma5_dip_watchlist_rules()
    return GAP_CONFIRMED_WATCHLIST_RULES


def load_tradable_symbols(max_symbols: int | None = None) -> list[str]:
    """从 Alpaca assets 中构建 active/tradable 普通股股票池。"""
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest

    client = build_trading_connection().client
    # US_EQUITY 仍包含权证、单位、ETF 等，需要按资产名称再过滤一次。
    request = GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
    assets = client.get_all_assets(request)
    symbols = sorted(
        str(getattr(asset, "symbol", "")).upper()
        for asset in assets
        if is_common_stock_asset(asset)
    )
    return symbols[:max_symbols] if max_symbols is not None else symbols


def refresh_watchlist_chart_from_watch_codes(
    settings: Settings | None = None,
    lookback_days: int = 60,
    batch_size: int = 100,
    feed: str = "sip",
):
    """以当前 watch_codes.txt 为唯一基准刷新 latest 图表页面。"""
    settings = settings or build_settings()
    watch_codes = read_watch_codes(settings.watch_codes_file)
    now_et = datetime.now(ZoneInfo(settings.market_timezone))
    symbols = [to_alpaca_symbol(code) for code in watch_codes]
    print(f"按 {settings.watch_codes_file.name} 刷新图表：codes={len(watch_codes)} feed={feed}", flush=True)
    bars_by_symbol = fetch_daily_bars(symbols, now_et, lookback_days, batch_size, feed) if symbols else {}
    chart_path = write_watchlist_chart_page(settings, [], bars_by_symbol)
    print(f"图表已按 {settings.watch_codes_file.name} 刷新：{chart_path}", flush=True)
    return chart_path


def is_common_stock_asset(asset) -> bool:
    """识别普通股，排除权证、单位、优先股、ETF/基金、ADR/ADS 等。"""
    symbol = str(getattr(asset, "symbol", "") or "").upper().strip()
    name = str(getattr(asset, "name", "") or "").lower()
    if not symbol or not getattr(asset, "tradable", False):
        return False
    if re.fullmatch(r"[A-Z]{5}", symbol):
        return False

    blocked_pattern = r"\b(warrants?|rights?|units?|preferred|preference|depositary|adr|ads|etfs?|etns?|funds?|trust|notes?|bonds?|debentures?|acquisition|spac|blank\s+check|shell\s+compan(?:y|ies))\b"
    if re.search(blocked_pattern, name):
        return False
    if re.search(r"\b(class\s+[b-z]|series)\b", name):
        return False

    common_keywords = ("common stock", "ordinary share", "ordinary shares", "common share", "common shares")
    if any(keyword in name for keyword in common_keywords):
        return True

    # Alpaca sometimes returns active tradable common stocks with only the company name.
    company_name_pattern = r"\b(inc|inc\.|incorporated|corp|corporation|company|co\.|ltd|limited|plc|group)\b"
    return bool(re.search(company_name_pattern, name))


def fetch_daily_bars(
    symbols: list[str],
    now_et: datetime,
    lookback_days: int,
    batch_size: int,
    feed: str,
) -> dict[str, list[DailyBar]]:
    """分批读取 Alpaca 日线；单批失败只跳过该批，避免整体中断。"""
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    api_key, secret_key = load_alpaca_credentials()
    client = StockHistoricalDataClient(api_key, secret_key)
    end = request_end_datetime(now_et, feed)
    start = end - timedelta(days=lookback_days)
    bars_by_symbol: dict[str, list[DailyBar]] = {}

    for batch in batched(symbols, batch_size):
        try:
            raw_bars = client.get_stock_bars(
                _bars_request(StockBarsRequest, TimeFrame, Adjustment, DataFeed, batch, start, end, lookback_days, feed)
            ).data
        except Exception as exc:
            if feed.lower() == "iex":
                print(f"日线读取失败，跳过 {batch[0]}...{batch[-1]}：{short_error(exc)}", flush=True)
                continue
            print(f"{feed.upper()} 日线读取失败，{batch[0]}...{batch[-1]} 改用 IEX：{short_error(exc)}", flush=True)
            fallback_end = request_end_datetime(now_et, "iex")
            fallback_start = fallback_end - timedelta(days=lookback_days)
            try:
                raw_bars = client.get_stock_bars(
                    _bars_request(StockBarsRequest, TimeFrame, Adjustment, DataFeed, batch, fallback_start, fallback_end, lookback_days, "iex")
                ).data
            except Exception as fallback_exc:
                print(f"IEX 日线读取失败，跳过 {batch[0]}...{batch[-1]}：{short_error(fallback_exc)}", flush=True)
                continue

        for symbol, bars in raw_bars.items():
            bars_by_symbol[symbol.upper()] = [daily_bar_from_alpaca(symbol.upper(), bar, now_et) for bar in bars]

    return bars_by_symbol


def _bars_request(StockBarsRequest, TimeFrame, Adjustment, DataFeed, batch, start, end, lookback_days: int, feed: str):
    """创建 Alpaca 日线请求，调用方负责 SIP 失败后的 IEX 降级。"""
    return StockBarsRequest(
        symbol_or_symbols=batch,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        limit=len(batch) * lookback_days,
        # 使用拆股调整日线，避免合股/拆股造成 MA 和图表严重失真。
        adjustment=Adjustment.SPLIT,
        feed=DataFeed(feed.lower()),
    )


def screen_candidates(
    bars_by_symbol: dict[str, list[DailyBar]],
    now_et: datetime,
    *,
    rules: WatchlistScreenRules | None = None,
) -> list[WatchCandidate]:
    """所有股票共用最近已收盘交易日作为 signal_date。"""
    rules = rules or GAP_CONFIRMED_WATCHLIST_RULES
    signal_date = latest_completed_signal_date(bars_by_symbol, now_et)
    if signal_date is None:
        return []

    candidates: list[WatchCandidate] = []
    for symbol, bars in bars_by_symbol.items():
        candidate = evaluate_watch_candidate(symbol, bars, now_et, signal_date, rules=rules)
        if candidate:
            candidates.append(candidate)
    return sorted(candidates, key=lambda item: (item.gain_pct, item.upper_shadow_pct), reverse=True)


def latest_completed_signal_date(bars_by_symbol: dict[str, list[DailyBar]], now_et: datetime) -> date | None:
    """从所有股票日线中找统一的最近已收盘交易日。"""
    dates = [
        bar.date
        for bars in bars_by_symbol.values()
        for bar in bars
        if is_completed_bar(bar, now_et)
    ]
    return max(dates) if dates else None


def evaluate_watch_candidate(
    symbol: str,
    bars: list[DailyBar],
    now_et: datetime,
    signal_date: date,
    *,
    rules: WatchlistScreenRules,
) -> WatchCandidate | None:
    """检查单股是否满足涨幅、MA5 动量、close/MA5 和 open/MA5。"""
    completed = [bar for bar in sorted(bars, key=lambda item: item.date) if is_completed_bar(bar, now_et)]
    signal_index = next((index for index, bar in enumerate(completed) if bar.date == signal_date), None)
    if signal_index is None or signal_index < 19:
        return None

    signal = completed[signal_index]
    previous = completed[signal_index - 1]
    if previous.close <= 0:
        return None

    closes20 = [bar.close for bar in completed[signal_index - 19 : signal_index + 1]]
    ma5 = average(closes20[-5:])
    ma10 = average(closes20[-10:])
    ma20 = average(closes20)
    previous_ma5 = average([bar.close for bar in completed[signal_index - 5 : signal_index]])
    gain_pct = signal.close / previous.close - 1.0
    ma5_gain_pct = ma5 / previous_ma5 - 1.0 if previous_ma5 > 0 else 0.0
    body_pct = bullish_body_pct(signal.open, signal.close)

    # 上影线只保留为诊断/排序字段，不再作为入选条件。
    upper_shadow_pct = (signal.high - max(signal.open, signal.close)) / previous.close

    if gain_pct <= rules.min_signal_gain_pct:
        return None
    if gain_pct - ma5_gain_pct <= rules.min_signal_gain_over_ma5_gain_pct:
        return None
    if ma5 <= 0 or signal.close / ma5 <= rules.min_close_to_ma5_ratio:
        return None
    if ma5 <= 0 or signal.close / ma5 > rules.max_close_to_ma5_ratio:
        return None
    if ma5 <= 0 or signal.open / ma5 <= rules.min_open_to_ma5_ratio:
        return None
    if rules.require_ma5_gt_ma10_gt_ma20 and not (ma5 > ma10 > ma20 > 0):
        return None
    if signal_close_position_pct(signal) < rules.min_signal_close_position_pct:
        return None

    return WatchCandidate(
        symbol,
        signal.date,
        gain_pct,
        upper_shadow_pct,
        ma5,
        ma10,
        ma20,
        signal.open,
        signal.high,
        signal.close,
        signal.low,
        ma5_gain_pct,
        body_pct,
    )


def validate_candidates(candidates: list[WatchCandidate], *, rules: WatchlistScreenRules | None = None) -> None:
    """写入前再次校验，防止异常数据进入真实监控列表。"""
    rules = rules or GAP_CONFIRMED_WATCHLIST_RULES
    for candidate in candidates:
        if candidate.ma5 <= 0 or candidate.close / candidate.ma5 <= rules.min_close_to_ma5_ratio:
            raise RuntimeError(f"{candidate.symbol} 收盘价不满足 close/MA5>{rules.min_close_to_ma5_ratio:g}")
        if candidate.ma5 <= 0 or candidate.close / candidate.ma5 > rules.max_close_to_ma5_ratio:
            raise RuntimeError(f"{candidate.symbol} 收盘价不满足 close/MA5<={rules.max_close_to_ma5_ratio:g}")
        if candidate.ma5 <= 0 or candidate.open / candidate.ma5 <= rules.min_open_to_ma5_ratio:
            raise RuntimeError(f"{candidate.symbol} 开盘价不满足 open/MA5>{rules.min_open_to_ma5_ratio:g}")
        if candidate.gain_pct <= rules.min_signal_gain_pct:
            raise RuntimeError(f"{candidate.symbol} 涨幅不满足 >{rules.min_signal_gain_pct:.0%}")
        if candidate.gain_pct - candidate.ma5_gain_pct <= rules.min_signal_gain_over_ma5_gain_pct:
            raise RuntimeError(f"{candidate.symbol} 涨幅不满足比 MA5 涨幅高 {rules.min_signal_gain_over_ma5_gain_pct:.0%} 以上")
        if rules.require_ma5_gt_ma10_gt_ma20 and not (candidate.ma5 > candidate.ma10 > candidate.ma20 > 0):
            raise RuntimeError(f"{candidate.symbol} 不满足 MA5>MA10>MA20")
        if signal_close_position_pct(candidate) < rules.min_signal_close_position_pct:
            raise RuntimeError(f"{candidate.symbol} 收盘位置不满足 >= {rules.min_signal_close_position_pct:.0%}")


def is_completed_bar(bar: DailyBar, now_et: datetime) -> bool:
    """判断日线是否已完成；16:15 ET 后才允许使用当天日线。"""
    if bar.date < now_et.date():
        return True
    after_close_buffer = now_et.hour > 16 or (now_et.hour == 16 and now_et.minute >= 15)
    return bar.date == now_et.date() and after_close_buffer


def average(values: list[float]) -> float:
    """计算简单移动平均。"""
    return sum(values) / len(values)


def bullish_body_pct(open_price: float, close_price: float) -> float:
    return close_price / open_price - 1.0 if open_price > 0 else -1.0


def signal_close_position_pct(bar: DailyBar | WatchCandidate) -> float:
    return (bar.close - bar.low) / (bar.high - bar.low) if bar.high > bar.low else 0.0


def daily_bar_from_alpaca(symbol: str, bar, now_et: datetime) -> DailyBar:
    """把 alpaca-py Bar 转成内部 DailyBar。"""
    bar_date = bar.timestamp.astimezone(now_et.tzinfo).date()
    return DailyBar(symbol, bar_date, float(bar.open), float(bar.high), float(bar.low), float(bar.close))


def request_end_datetime(now_et: datetime, feed: str = "sip") -> datetime:
    """计算日线请求 end；SIP 需要避开 recent data 权限窗口。"""
    return daily_request_end(now_et, feed)


def stale_sip_end(now_et: datetime, boundary: datetime) -> datetime:
    """把 SIP 请求时间压到 20 分钟前，避免免费权限错误。"""
    return stale_sip_daily_end(now_et, boundary)


def write_watch_codes(path: Path, candidates: list[WatchCandidate], *, rules: WatchlistScreenRules | None = None) -> None:
    """写出监控直接读取的 watch_codes.txt。"""
    rules = rules or GAP_CONFIRMED_WATCHLIST_RULES
    ma_order_text = "MA5>MA10>MA20" if rules.require_ma5_gt_ma10_gt_ma20 else "MA order not required"
    lines = [
        "# Auto-generated by watchcode_ma5.py",
        "# Pool: Alpaca active/tradable regular common stocks only",
        f"# Rules: gain>{rules.min_signal_gain_pct:.0%}, signal_gain>ma5_gain+{rules.min_signal_gain_over_ma5_gain_pct:.0%}, close/MA5>{rules.min_close_to_ma5_ratio:g}, close/MA5<={rules.max_close_to_ma5_ratio:g}, open/MA5>{rules.min_open_to_ma5_ratio:g}, close_position>={rules.min_signal_close_position_pct:.0%}, {ma_order_text}",
    ]
    if candidates:
        lines.append(f"# signal_date={candidates[0].signal_date}")
    lines.extend(f"US.{candidate.symbol}" for candidate in candidates)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_candidate_report(output_dir: Path, candidates: list[WatchCandidate]) -> None:
    """写出候选诊断 CSV，方便复盘入选原因。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    day = candidates[0].signal_date if candidates else datetime.now().date()
    path = output_dir / f"watch_candidates_{day}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["symbol", "signal_date", "gain_pct", "ma5_gain_pct", "body_pct", "upper_shadow_pct", "ma5", "ma10", "ma20", "open", "high", "low", "close"],
        )
        writer.writeheader()
        for candidate in candidates:
            writer.writerow(candidate.__dict__)


def batched(values: list[str], size: int):
    """把股票列表切成 Alpaca API 批次。"""
    for start in range(0, len(values), size):
        yield values[start : start + size]
